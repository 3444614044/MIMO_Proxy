# -*- coding: utf-8 -*-
"""mimo-claw 容器定时调度

规则（用户指定）：
  · 一个号一个建 —— 串行处理，账号之间留间隔，避免同时撞创建上限
  · 一个号一天只能建一次 —— 按自然日计数，跨天自动重置
  · 每 N 小时跑一轮 —— 默认 3 小时

设计要点：
  · 只有真正创建成功（AVAILABLE）才计入当日次数；失败不计数，
    下一轮（3 小时后）自然重试，不会疯狂请求。
  · 已有可用容器直接跳过，也不消耗次数。
  · 状态落盘（claw_state.json），重启后仍记得今天建过几次。

用法：config.json 的 claw.auto_schedule = true 开启。
"""

import json
import os
import threading
import time


def _today(ts=None):
    """本地自然日，用于每日限额；跨天自动重置"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class ClawScheduler:
    def __init__(self, pool, cfg, state_path="claw_state.json", log=print):
        self.pool = pool
        self.cfg = cfg
        self.state_path = state_path
        self.log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._th = None
        self._state = self._load()
        self._last_run = 0.0
        self._started = 0.0
        self._running = False
        self._history = []

    # ---------------- 状态持久化 ----------------
    def _load(self):
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return dict(d.get("accounts") or {})
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"accounts": self._state,
                           "updated": int(time.time())},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except Exception as e:      # noqa: BLE001
            self.log(f"claw 调度状态保存失败: {e}")

    # ---------------- 每日限额 ----------------
    def _used_today(self, uid):
        st = self._state.get(str(uid)) or {}
        if st.get("date") != _today():
            return 0        # 跨天 -> 视为 0
        return int(st.get("count") or 0)

    def _bump(self, uid, status, ok):
        """ok=True 才累加次数；任何情况都更新最近一次时间与状态"""
        key = str(uid)
        st = self._state.get(key) or {}
        if st.get("date") != _today():
            st = {"date": _today(), "count": 0}
        if ok:
            st["count"] = int(st.get("count") or 0) + 1
        st["last_ts"] = int(time.time())
        st["last_status"] = status
        st["last_ok"] = bool(ok)
        self._state[key] = st

    # ---------------- 配置 ----------------
    @property
    def interval(self):
        try:
            h = float(self.cfg.get("claw", "schedule_interval_hours", 3) or 3)
            return max(60.0, h * 3600)
        except Exception:
            return 3 * 3600

    @property
    def only_one(self):
        """只要有一个号有可用容器就够 —— 默认开启"""
        v = self.cfg.get("claw", "only_one_container")
        return True if v is None else bool(v)

    @property
    def daily_limit(self):
        try:
            return max(1, int(self.cfg.get("claw", "daily_limit_per_account", 1) or 1))
        except Exception:
            return 1

    # ---------------- 生命周期 ----------------
    def start(self, run_now=False):
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._started = time.time()
        self._th = threading.Thread(target=self._loop, daemon=True,
                                    name="claw-sched")
        self._th.start()
        if run_now:
            threading.Thread(target=self.tick, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:      # noqa: BLE001
                self.log(f"claw 调度异常: {str(e)[:120]}")

    # ---------------- 一轮 ----------------
    def tick(self, uid=None, ignore_limit=False, force=False):
        with self._lock:
            if self._running:
                return {"skipped": "上一轮尚未结束"}
            self._running = True
        try:
            return self._tick(uid, ignore_limit, force)
        finally:
            self._running = False
            self._last_run = time.time()

    def _tick(self, uid=None, ignore_limit=False, force=False):
        import mimo_proxy.mimo as M

        accounts = list(getattr(self.pool, "accounts", []) or [])
        if uid:
            accounts = [a for a in accounts if str(a.uid) == str(uid)]
        try:
            gap = float(self.cfg.get("claw", "schedule_account_gap", 3) or 0)
        except Exception:
            gap = 3.0
        try:
            timeout = int(self.cfg.get("claw", "create_wait_timeout", 90) or 90)
        except Exception:
            timeout = 90

        limit = self.daily_limit
        only_one = self.only_one
        import mimo_proxy.mimo as _M

        # ★ 全局只需一个：先扫一遍，已有任一 AVAILABLE 就整轮跳过
        if only_one and not uid and not force:
            for a in accounts:
                try:
                    if (getattr(a, "valid", True)
                            and (_M.claw_container(a) or {}).get("status") == "AVAILABLE"):
                        return {"ran": True, "skipped": True,
                                "reason": "已有账号持有可用容器，无需再建",
                                "holder": str(getattr(a, "uid", "")),
                                "results": []}
                except Exception:      # noqa: BLE001
                    continue

        results = []
        first = True
        for a in accounts:
            if self._stop.is_set():
                break
            # ★ 一个号一个建：账号之间留间隔，别并发撞上限
            if not first and gap > 0:
                time.sleep(gap)
            first = False

            uid_s = str(getattr(a, "uid", ""))
            item = {"uid": uid_s}
            try:
                # 1) 每日限额（自然日）
                used = self._used_today(uid_s)
                if not ignore_limit and used >= limit:
                    item["action"] = "skip"
                    item["reason"] = f"今日已建 {used} 次，上限 {limit}"
                    results.append(item)
                    continue

                # 2) 已有可用容器 -> 跳过，不消耗次数
                st = M.claw_container(a) or {}
                status = st.get("status")
                if status == "AVAILABLE":
                    item["action"] = "skip"
                    item["status"] = status
                    item["reason"] = "已有可用容器"
                    self._bump(uid_s, status, ok=False)
                    results.append(item)
                    continue

                # 3) 申请并等待就绪
                res = M.ensure_container(a, auto_create=True, wait=True,
                                         timeout=timeout) or {}
                st2 = res.get("status")
                ok = (st2 == "AVAILABLE")
                item["action"] = "create"
                item["status"] = st2
                if res.get("error"):
                    item["error"] = str(res["error"])[:150]
                    ok = False
                # ★ 只有成功才计次；失败下一轮自然重试
                self._bump(uid_s, st2, ok=ok)
                item["created_today"] = self._used_today(uid_s)
                # ★ 全局只需一个：建成就停，不再给后面的号建
                if ok and only_one and not force:
                    item["note"] = "已有容器，本轮停止"
                    results.append(item)
                    self._save()
                    self._history.insert(0, {"ts": int(time.time()),
                                             "results": results})
                    self._history = self._history[:5]
                    return {"ran": True, "stopped": True, "results": results}
            except Exception as e:      # noqa: BLE001
                item["action"] = "error"
                item["error"] = str(e)[:150]
            results.append(item)

        self._save()
        self._history.insert(0, {"ts": int(time.time()), "results": results})
        self._history = self._history[:5]
        return {"ran": True, "results": results}

    # ---------------- 查询 ----------------
    def status(self):
        base = self._last_run or self._started or time.time()
        accs = []
        for a in getattr(self.pool, "accounts", []) or []:
            uid_s = str(getattr(a, "uid", ""))
            st = self._state.get(uid_s) or {}
            accs.append({
                "uid": uid_s,
                "date": st.get("date"),
                "created_today": self._used_today(uid_s),
                "limit": self.daily_limit,
                "last_ts": st.get("last_ts"),
                "last_status": st.get("last_status"),
            })
        return {
            "enabled": bool(self._th and self._th.is_alive()),
            "running": self._running,
            "interval_hours": round(self.interval / 3600.0, 2),
            "daily_limit": self.daily_limit,
            "last_run": int(self._last_run) if self._last_run else None,
            "next_run": int(base + self.interval),
            "today": _today(),
            "accounts": accs,
            "history": self._history[:3],
        }

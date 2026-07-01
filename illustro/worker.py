"""Background worker: runs incremental builds in a separate thread with pause/stop/trigger support.

Design goals (for large collections, slow processing):
- Stoppable anytime: build checks stop_check per batch; processed items are saved, resumable next run.
- Pause = abort the current round (unwinds build, releasing the tagger/model from memory);
  Resume = start a fresh round (DB-level incrementality skips already-tagged images).
- Graceful shutdown on process exit (docker stop / Ctrl+C), no hangs.
- Status/progress queryable via API for UI display.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .config import Config
from .pipeline import build


class Worker:
    # Maximum number of round summaries kept in the in-memory ring buffer
    MAX_ROUNDS = 50

    def __init__(self, cfg: Config, interval: int = 1800, autostart: bool = True):
        self.cfg = cfg
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()     # Full stop (exit thread)
        self._pause = threading.Event()    # Pause (suspend processing)
        self._wake = threading.Event()     # Interrupt sleep / trigger immediately
        self._lock = threading.Lock()
        # State
        self.phase = "idle"                # scanning/tagging/indexing/sleeping/paused/stopped/idle
        self.processed = 0
        self.total = 0
        self.last_run = 0.0
        self.last_error: Optional[str] = None
        # Round tracking for monitoring
        self._round_start = 0.0            # Wall-clock start of the current round
        self._rounds: list[dict] = []      # Ring buffer of completed round summaries
        if autostart:
            self.start()

    # ---------- Lifecycle ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="illustro-worker", daemon=True)
        self._thread.start()

    def stop(self, join: bool = True, timeout: float = 60.0):
        """Request stop: interrupt current batch and sleep, wait for thread to exit."""
        self._stop.set()
        self._wake.set()
        if join and self._thread:
            self._thread.join(timeout=timeout)

    def pause(self):
        self._pause.set()
        self._wake.set()   # Immediately interrupt current processing/sleep

    def resume(self):
        self._pause.clear()
        self._wake.set()

    def run_now(self):
        """Start a round immediately (skip remaining sleep)."""
        self._wake.set()

    # ---------- Internal ----------
    def _should_stop(self) -> bool:
        # Both stop and pause cause the current build to finish as quickly as possible
        return self._stop.is_set() or self._pause.is_set()

    def _progress(self, phase: str, done: int, total: int):
        with self._lock:
            self.phase = phase
            self.processed = done
            self.total = total

    def _run(self):
        while not self._stop.is_set():
            if self._pause.is_set():
                with self._lock:
                    self.phase = "paused"
                # Block until resume() or stop() sets _wake; no busy polling.
                self._wake.wait()
                self._wake.clear()
                continue
            with self._lock:
                self.phase = "running"
                self.processed = 0
                self.total = 0
                self.last_error = None
                self._round_start = time.time()
            round_processed = 0
            try:
                build(self.cfg, stop_check=self._should_stop, progress_cb=self._progress)
            except Exception as e:  # Don't let the thread die on errors; log and continue the loop
                with self._lock:
                    self.last_error = f"{type(e).__name__}: {e}"
                print(f"[worker] Processing error: {self.last_error}")
            interrupted = self._should_stop()
            with self._lock:
                round_end = time.time()
                round_processed = self.processed
                # Only record a real completion when the round wasn't interrupted by pause/stop
                if not interrupted:
                    self.last_run = round_end
                # Append round summary to ring buffer (covers both completed and interrupted rounds)
                self._rounds.append({
                    "start": self._round_start,
                    "end": round_end,
                    "duration_sec": round(round_end - self._round_start, 2),
                    "processed": round_processed,
                    "interrupted": interrupted,
                    "error": self.last_error,
                })
                if len(self._rounds) > self.MAX_ROUNDS:
                    self._rounds = self._rounds[-self.MAX_ROUNDS:]
            if self._stop.is_set():
                break
            # Sleep that can be interrupted by pause/resume/run_now/stop
            with self._lock:
                self.phase = "sleeping"
            self._wake.wait(self.interval)
            self._wake.clear()
        with self._lock:
            self.phase = "stopped"

    # ---------- Status ----------
    def status(self) -> dict:
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            if self._stop.is_set():
                state = "stopped"
            elif self._pause.is_set():
                state = "paused"
            elif self.phase in ("scanning", "tagging", "indexing", "applying_zh", "running"):
                state = "running"
            elif self.phase == "sleeping":
                state = "sleeping"
            else:
                state = "idle"
            return {
                "state": state,
                "phase": self.phase,
                "paused": self._pause.is_set(),
                "alive": alive,
                "processed": self.processed,
                "progress_total": self.total,   # Items to process this round (distinct from DB total image count)
                "last_run": self.last_run,
                "last_error": self.last_error,
                "interval": self.interval,
            }

    def monitor_status(self) -> dict:
        """Extended status for the monitoring dashboard: adds round timing, throughput, ETA, and history."""
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            if self._stop.is_set():
                state = "stopped"
            elif self._pause.is_set():
                state = "paused"
            elif self.phase in ("scanning", "tagging", "indexing", "applying_zh", "running"):
                state = "running"
            elif self.phase == "sleeping":
                state = "sleeping"
            else:
                state = "idle"
            now = time.time()
            elapsed = now - self._round_start if self._round_start else 0.0
            throughput = (self.processed / elapsed * 60) if elapsed > 0.1 and self.processed > 0 else 0.0
            eta = ((self.total - self.processed) / throughput * 60) if throughput > 0 and self.total > self.processed else 0.0
            return {
                "state": state,
                "phase": self.phase,
                "paused": self._pause.is_set(),
                "alive": alive,
                "processed": self.processed,
                "progress_total": self.total,
                "last_run": self.last_run,
                "last_error": self.last_error,
                "interval": self.interval,
                "round_start": self._round_start,
                "elapsed_sec": round(elapsed, 2),
                "throughput_img_min": round(throughput, 1),
                "eta_sec": round(eta, 1),
                "rounds": list(self._rounds),
            }

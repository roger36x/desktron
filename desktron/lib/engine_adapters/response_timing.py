"""回合关键路径计时；只留毫秒与阶段，不记录提示词、工具参数或凭证。"""
import time


class ResponseTiming:
    def __init__(self):
        self.started = time.monotonic()
        self.marks = {}

    def mark(self, stage):
        if stage not in self.marks:
            self.marks[stage] = round((time.monotonic() - self.started) * 1000, 2)


def mark(ctx, stage):
    timing = ctx.get("_response_timing")
    if timing is not None:
        timing.mark(stage)

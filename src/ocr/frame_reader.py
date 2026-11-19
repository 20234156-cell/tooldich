"""Bounded forward decoding without changing requested source frame indices."""
import cv2


class FrameReader:
    def __init__(self, capture, check, max_forward=48):
        self.capture = capture
        self.check = check
        self.max_forward = max_forward
        self.stats = dict(seeks=0, grabs=0, reads=0)

    def read(self, index):
        self.check()
        cap = self.capture
        next_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        gap = index-next_index
        if 0 <= gap <= self.max_forward and hasattr(cap, 'grab'):
            for _ in range(gap):
                self.check()
                if not cap.grab():
                    return False, None
                self.stats['grabs'] += 1
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            self.stats['seeks'] += 1
        self.check()
        self.stats['reads'] += 1
        return cap.read()

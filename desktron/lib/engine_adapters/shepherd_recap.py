"""Recap belongs to the public Conversation, never to a mutable active worker."""
import copy

from .recap import _want_autotitle


class ShepherdRecapMixin:
    def _recap_snapshot(self, sid, ctx):
        snap = super()._recap_snapshot(sid, ctx)
        # Private IDs may only travel to the isolated native generator, not title
        # aliases, public session indexes, journal attribution or task events.
        snap['engine_sid'] = ''
        active = self.service.active(sid)
        snap['_shepherd_source'] = copy.deepcopy(active)
        # Previously unsupported conversations get one chance on their next clean
        # boundary, regardless of age; existing/manual names still win.
        snap['want_title'] = _want_autotitle(sid, 1, '')
        return snap

    def _recap_context_ready(self, sid, snap):
        expected = snap.get('_shepherd_source') or {}
        try:
            state = expected['state']
            stable = bool(expected.get('binding') and expected.get('lease')
                          and state.get('committed_offset', 0) > 0
                          and not state.get('inflight') and not state.get('pending_switch'))
            if snap.get('source') == getattr(self, 'SRC_PEER', 'peer'):
                # Peer asks are bound to this immutable coordinator snapshot.
                # Once their disposable native view is prepared, later user
                # turns may advance the live conversation without invalidating it.
                return stable and sid in self.index
            current = self.service.active(sid)
            return stable and current == expected
        except Exception:
            return False

    def recap_generate(self, sid, run, snap):
        from shepherd.recap import generate
        if not self._recap_context_ready(sid, snap):
            raise RuntimeError('Shepherd recap 来源已变化')
        return generate(self, sid, run, snap)

    def _recap_accept(self, sid, seq, snap, run):
        if not self._recap_context_ready(sid, snap):
            raise RuntimeError('Shepherd recap 来源已变化，结果未提交')

"""Explicit restart stops dispatch without requiring conversational completion."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
from engine_adapters.cli_turn import TurnRunnerMixin
from engine_adapters.shepherd import ShepherdAdapter
from shepherd import Conflict


def test_queued_prompt_is_rejected_before_hook_or_message_write():
    ad = SimpleNamespace(_lock=threading.Lock(), _turn_locks={},
        _recap_cancel=mock.Mock(), _before_turn_start=mock.Mock(),
        _run=mock.Mock(), bus=SimpleNamespace(publish=mock.Mock()))
    with mock.patch('self_update.external_draining', return_value=True), \
         pytest.raises(RuntimeError, match='排队消息未发送'):
        TurnRunnerMixin._run_serial(ad, 'sid', 'keep my draft', client_request_id='request_1')
    ad._before_turn_start.assert_not_called()
    ad._run.assert_not_called()
    event = ad.bus.publish.call_args.args[0]
    assert event['properties']['requestID'] == 'request_1'
    assert event['properties']['error']['code'] == 'prompt_not_started'
    assert not ad._turn_locks['sid'].locked()


def test_recovery_under_existing_guard_is_deferred_without_native_writes():
    recovery = SimpleNamespace(recover_guarded=mock.Mock())
    guard = threading.Lock()
    ad = SimpleNamespace(service=SimpleNamespace(coordinator=object()),
                         _recovery=recovery, _switch_lock=lambda _:guard)
    with mock.patch('self_update.external_draining', return_value=True):
        result = ShepherdAdapter._recover_with_turn_guard(ad, 'sid', trigger='turn_finished')
    assert result['action'] == 'deferred'
    recovery.recover_guarded.assert_not_called()
    assert not guard.locked()


def test_pending_engine_selection_survives_restart_without_catchup():
    service = mock.Mock()
    ad = SimpleNamespace(_shutting_down=False, _recap_cancel=mock.Mock(),
                         _require_execution=lambda _sid: None,
                         _switch_lock=lambda _:threading.Lock(), service=service,
                         index={'sid':{'next_engine':'pi'}})
    with mock.patch('self_update.external_draining', return_value=True), \
         pytest.raises(Conflict, match='引擎选择已保留'):
        ShepherdAdapter._apply_requested_engine(ad, 'sid')
    assert ad.index['sid']['next_engine'] == 'pi'
    assert service.mock_calls == []


def test_turn_thread_failure_balances_only_its_conversation_registration():
    ad = SimpleNamespace(_recap_cancel=mock.Mock(), _peer_user_admit=mock.Mock(),
                         _peer_user_release=mock.Mock())
    ad._prompt_user_async = lambda *args, **kwargs: TurnRunnerMixin._prompt_user_async(
        ad, *args, **kwargs)
    with mock.patch('self_update.begin_work', return_value=True) as begin, \
         mock.patch('self_update.finish_work') as finish, \
         mock.patch('threading.Thread.start', side_effect=RuntimeError('thread start failed')), \
         pytest.raises(RuntimeError, match='thread start failed'):
        TurnRunnerMixin.prompt_async(ad, 'sid', 'hello')
    begin.assert_called_once_with(user_activity=True, restart_blocking=False)
    finish.assert_called_once_with(restart_blocking=False)
    ad._peer_user_admit.assert_called_once_with('sid', '')
    ad._peer_user_release.assert_called_once_with('sid')

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import btc_clock_hybrid_core as hybrid
import btc_clock_hybrid_runtime as runtime
import btc_fixed_advisory as btc
import isa_leverage_core as isa
import isa_leverage_messages as messages
import portfolio_operational_alerts as ops
import portfolio_operational_delivery as delivery
import portfolio_state_guard as guard
import portfolio_telegram_bot as app


class ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.install()

    def setUp(self):
        self.monday = datetime(2026, 9, 7, 0, 17, tzinfo=UTC)
        self.sunday = self.monday - timedelta(days=1)
        self.state = btc._initial_state(self.sunday)

    def block(self, offset, now=None, epoch=4):
        height = epoch * hybrid.INTERVAL + offset
        return btc.BlockContext(height, epoch, offset / hybrid.INTERVAL, height, height, btc.iso_utc(now or self.monday))

    def create(self, block, now):
        return btc.create_official_order(self.state, block=block, price_krw=100_000_000, now_utc=now)

    def test_missed_monday_recovers_once_on_tuesday_with_pre_due_evidence(self):
        btc.detect_block_events(self.state, self.block(hybrid.ENTRY, self.sunday), self.sunday)
        now = self.monday + timedelta(days=1)
        self.assertTrue(btc.official_action_due(now, self.state))
        action = self.create(self.block(hybrid.ENTRY + 100, now), now)
        self.assertEqual(action['instruction'].step, 1)
        self.assertEqual(action['pending_sync']['official_execution']['mode'], 'catch_up')
        self.assertEqual(self.state['strategy']['last_official_monday'], '2026-09-07')
        self.assertIn('지연 점검', action['instruction'].reason)
        btc.complete_pending_sync(self.state, btc_quantity=0.0333, cash_krw=6_670_000, now_utc=now)
        self.assertFalse(btc.official_action_due(now + timedelta(hours=1), self.state))
        self.assertIsNone(self.create(self.block(hybrid.ENTRY + 200, now), now))

    def test_new_threshold_first_seen_tuesday_is_not_backdated_to_monday(self):
        now = self.monday + timedelta(days=1)
        block = self.block(hybrid.ENTRY, now)
        btc.detect_block_events(self.state, block, now)
        action = self.create(block, now)
        self.assertEqual(action['type'], 'CHECK_DEFERRED')
        self.assertIsNone(self.state['telegram']['pending_sync'])
        self.assertEqual(self.state['strategy']['phase'], 'WAITING_ENTRY')

    def test_missing_eligibility_history_is_conservative_not_guessed(self):
        action = self.create(self.block(hybrid.ENTRY), self.monday + timedelta(days=1))
        self.assertEqual(action['type'], 'CHECK_DEFERRED')

    def test_before_monday_due_never_replays_previous_week(self):
        self.assertFalse(btc.official_action_due(self.monday - timedelta(minutes=1), self.state))
        self.assertIsNone(self.create(self.block(hybrid.ENTRY), self.monday - timedelta(minutes=1)))

    def test_crossed_all_exit_thresholds_recovers_only_first_stage(self):
        self.state['strategy'].update(phase='HOLD', cycle_epoch=3, entry_steps_completed=3)
        self.state['account'].update(cash_krw=0, btc_quantity=1)
        block = self.block(hybrid.EXITS[2] + 100, self.sunday)
        btc.detect_block_events(self.state, block, self.sunday)
        now = self.monday + timedelta(days=1)
        action = self.create(block, now)
        self.assertEqual((action['instruction'].kind, action['instruction'].step), ('EXIT', 1))
        self.assertIsNone(self.create(block, now + timedelta(minutes=15)))
        self.assertEqual(self.state['strategy']['exit_steps_completed'], 0)

    def test_exit_36_9_does_not_create_second_exit(self):
        self.state['strategy'].update(phase='EXIT', cycle_epoch=3, exit_steps_completed=1)
        self.state['account'].update(cash_krw=30_000_000, btc_quantity=0.7)
        block = self.block(round(210_000 * .369), self.sunday)
        btc.detect_block_events(self.state, block, self.sunday)
        self.assertIsNone(self.create(block, self.monday + timedelta(days=1)))

    def test_pending_sync_blocks_recovery_without_changing_it(self):
        self.state['telegram']['pending_sync'] = {'kind': 'ENTRY', 'step': 1, 'id': 'unchanged'}
        before = deepcopy(self.state['telegram']['pending_sync'])
        action = self.create(self.block(hybrid.ENTRY), self.monday + timedelta(days=1))
        self.assertEqual(action['type'], 'SYNC_BLOCK')
        self.assertEqual(self.state['telegram']['pending_sync'], before)

    def test_several_missing_weeks_never_replay_multiple_orders(self):
        btc.detect_block_events(self.state, self.block(hybrid.ENTRY, self.sunday), self.sunday)
        now = self.monday + timedelta(days=23)
        action = self.create(self.block(hybrid.ENTRY + 1000, now), now)
        self.assertEqual(action['instruction'].step, 1)
        self.assertEqual(self.state['strategy']['last_official_monday'], '2026-09-28')

    def test_three_day_funding_alert_suppresses_later_stale_five_day_alert(self):
        wednesday = datetime(2026, 9, 9, 0, 17, tzinfo=UTC)
        block = self.block(hybrid.ENTRY - 3 * 144, wednesday)
        events = btc.detect_block_events(self.state, block, wednesday)
        prep = [e for e in events if e['type'] == 'ENTRY_FUNDING_PREP']
        self.assertEqual(prep[0]['lead_business_days'], 3)
        self.assertIn('4:5', self.state['strategy']['entry_funding_alerts_sent'])
        with patch.object(runtime, 'business_days_until', return_value=5):
            events = btc.detect_block_events(self.state, block, wednesday + timedelta(hours=1))
        self.assertFalse([e for e in events if e['type'] == 'ENTRY_FUNDING_PREP'])

    def isa_state(self, now=None, complete=True):
        state = isa.new_state(now or self.sunday)
        state['strategy'].update(initial_plan_sent=True, initial_completed=complete, monthly_start_period='2026-09')
        return state

    def test_isa_outer_gate_allows_afternoon_catch_up(self):
        now = self.monday + timedelta(hours=5)
        state = self.isa_state()
        self.assertTrue(app._isa_outbound_due(state, now_utc=now, initialized_new=False, force_status=False))
        self.assertTrue(isa.is_monthly_plan_due(state, now_kst=now.astimezone(isa.KST), latest_quote_date='2026-09-04'))

    def test_isa_no_pre0917_advice_and_no_future_or_old_quotes(self):
        state = self.isa_state()
        self.assertFalse(isa.is_monthly_plan_due(state, now_kst=(self.monday - timedelta(minutes=1)).astimezone(isa.KST), latest_quote_date='2026-09-04'))
        for date in ('2026-09-08', '2026-08-31'):
            self.assertFalse(isa.is_monthly_plan_due(state, now_kst=self.monday.astimezone(isa.KST), latest_quote_date=date))

    def test_isa_same_month_dedup_and_initial_incomplete_gate(self):
        state = self.isa_state()
        state['strategy']['last_monthly_plan_period'] = '2026-09'
        self.assertFalse(isa.is_monthly_plan_due(state, now_kst=self.monday.astimezone(isa.KST), latest_quote_date='2026-09-04'))
        state = self.isa_state(complete=False)
        self.assertFalse(isa.is_monthly_plan_due(state, now_kst=self.monday.astimezone(isa.KST), latest_quote_date='2026-09-04'))

    def test_isa_hourly_retry_only_for_unresolved_month_or_error(self):
        state = self.isa_state()
        state['data'].update(last_check_at_utc=self.monday.isoformat(), status='ok')
        for minutes, expected in ((15, False), (60, True)):
            self.assertEqual(app._isa_outbound_due(state, now_utc=self.monday+timedelta(minutes=minutes), initialized_new=False, force_status=False), expected)
        state['strategy']['last_monthly_plan_period']='2026-09'
        self.assertFalse(app._isa_outbound_due(state, now_utc=self.monday+timedelta(hours=3), initialized_new=False, force_status=False))
        self.assertTrue(app._isa_outbound_due(state, now_utc=self.monday+timedelta(minutes=1), initialized_new=False, force_status=True))

    @staticmethod
    def quotes():
        return {h['code']: isa.QuoteSnapshot(h['code'], h['name'], '2026-09-04', 40_000) for h in (*isa.EXISTING_HOLDINGS, {'code':isa.TIGER_CODE, 'name':isa.TIGER_NAME})}

    def test_partial_initial_fill_reduces_additional_order_budget(self):
        state = self.isa_state(complete=False)
        state['account'].update(tiger_quantity=100, tiger_invested_krw=4_000_000)
        text = messages.initial_plan_message(state, self.quotes(), None)
        self.assertIn('잔여예산: 6,000,000원', text)
        self.assertIn('주문 검토수량: 150주', text)
        self.assertNotIn('주문 검토수량: 250주', text)
        state['account']['tiger_invested_krw']=10_000_000
        self.assertIn('추가 주문 대신', messages.initial_plan_message(state, self.quotes(), None))

    def test_invalid_initial_cost_does_not_generate_duplicate_buy(self):
        state = self.isa_state(complete=False)
        state['account']['tiger_quantity']=20
        with self.assertRaises(isa.IsaStrategyError):
            isa.remaining_initial_budget(state)

    def process_ops(self, gap_minutes, *, sensitive=False, event='schedule', fail=False, previous_dispatch=False):
        now = self.monday + timedelta(days=1)
        state = deepcopy(self.state)
        state['operations']={
            'schema_version':1,
            'last_weekly_heartbeat_period':'2026-W37',
            'last_scheduled_run_at_utc':btc.iso_utc(now-timedelta(days=1)),
            'last_service_completed_at_utc':btc.iso_utc(now-timedelta(minutes=gap_minutes)),
        }
        if sensitive:
            state['telegram']['conversation']={'type':'isa_sync_tiger_quantity'}
        class Client:
            def __init__(self): self.messages=[]
            def send_message(self, text, **kwargs):
                if fail: raise RuntimeError('test network failure')
                self.messages.append(text)
        client=Client()
        with tempfile.TemporaryDirectory() as d:
            bp,ip=Path(d)/'btc.json',Path(d)/'isa.json'
            btc.save_state(bp,state);isa.save_state(ip,self.isa_state(complete=False),now_utc=now)
            result=ops.process_operational_alerts(btc_state_path=bp,isa_state_path=ip,
                btc_result={'data_status':'ok'},isa_result={'data_status':'ok'},now_utc=now,
                event_name=event,client=client)
            loaded,_=btc.load_state(bp,now_utc=now)
        return result,client.messages,loaded

    def test_quiet_151_minute_gap_is_logged_without_telegram_warning(self):
        result,texts,_=self.process_ops(151)
        self.assertFalse(result['gap_alert_sent'])
        self.assertEqual(result['schedule_gap_minutes'],151)
        self.assertEqual(texts,[])

    def test_sensitive_151_minute_gap_warns(self):
        result,texts,_=self.process_ops(151,sensitive=True)
        self.assertTrue(result['gap_alert_sent'])
        self.assertIn('GitHub 전체 장애 여부는 확인되지 않았습니다',texts[0])

    def test_quiet_4_hour_gap_warns_and_dispatch_updates_clock(self):
        result,texts,loaded=self.process_ops(240,event='workflow_dispatch')
        self.assertTrue(result['gap_alert_sent'])
        self.assertEqual(loaded['operations']['last_execution_event'],'workflow_dispatch')

    def test_recent_dispatch_prevents_false_schedule_gap_alarm(self):
        result,texts,_=self.process_ops(15)
        self.assertFalse(result['gap_alert_sent'])
        self.assertEqual(texts,[])

    def test_cache_miss_and_mixed_revision_fail_closed(self):
        for bk,ik in (('', ''),('btc-fixed-six-state-1-1','isa-tiger-leverage-state-2-1')):
            with self.assertRaises(ValueError): guard.validate_cache_pair(bk,ik)
        guard.validate_cache_pair('btc-fixed-six-state-123-1','isa-tiger-leverage-state-123-1')

    def test_missing_state_and_nonfinite_isa_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'isa.json'
            with self.assertRaises(ValueError): guard.read_valid_state(path,'ISA')
            state=self.isa_state();state['account']['tiger_quantity']=float('nan')
            path.write_text(json.dumps(state))
            with self.assertRaises(ValueError): guard.read_valid_state(path,'ISA')

    def test_watchdog_dispatch_does_not_trigger_weekend_manual_heartbeat(self):
        saturday=datetime(2026,9,5,0,17,tzinfo=UTC)
        with patch.dict('os.environ', {'GITHUB_EVENT_NAME':'workflow_dispatch','QG_TRIGGER_SOURCE':'cloudflare_watchdog'}):
            self.assertFalse(delivery.deployment_aware_heartbeat_due({},saturday))

    def test_failed_warning_delivery_does_not_mark_sent(self):
        now = self.monday + timedelta(days=1)
        with tempfile.TemporaryDirectory() as d:
            bp, ip = Path(d)/'btc.json', Path(d)/'isa.json'
            state = deepcopy(self.state)
            state['operations'] = {
                'schema_version': 1,
                'last_service_completed_at_utc': btc.iso_utc(now-timedelta(hours=5)),
                'last_weekly_heartbeat_period': '2026-W37',
            }
            btc.save_state(bp, state)
            isa.save_state(ip, self.isa_state(complete=False), now_utc=now)
            class BrokenClient:
                def send_message(self, *args, **kwargs):
                    raise RuntimeError('mock network error')
            with self.assertRaises(RuntimeError):
                ops.process_operational_alerts(
                    btc_state_path=bp, isa_state_path=ip,
                    btc_result={'data_status':'ok'}, isa_result={'data_status':'ok'},
                    now_utc=now, event_name='schedule', client=BrokenClient())
            saved=json.loads(bp.read_text())
            self.assertNotIn('last_gap_alert_at_utc', saved['operations'])
            self.assertEqual(saved['operations']['last_service_completed_at_utc'], btc.iso_utc(now-timedelta(hours=5)))

    def test_skipped_isa_with_prior_error_cannot_be_reported_healthy(self):
        now=self.monday+timedelta(days=1)
        with tempfile.TemporaryDirectory() as d:
            bp, ip = Path(d)/'btc.json', Path(d)/'isa.json'
            state=deepcopy(self.state)
            state['operations']={'schema_version':1,'last_weekly_heartbeat_period':'2026-W37'}
            btc.save_state(bp,state);isa.save_state(ip,self.isa_state(complete=False),now_utc=now)
            result=ops.process_operational_alerts(
                btc_state_path=bp,isa_state_path=ip,btc_result={'data_status':'ok'},
                isa_result={'data_status':'error','data_checked_this_run':False},
                now_utc=now,event_name='schedule')
            self.assertFalse(result['data_checks_ok'])

    def test_state_guard_preflight_and_finalize_preserve_account(self):
        with tempfile.TemporaryDirectory() as d:
            bp,ip,baseline=Path(d)/'btc.json',Path(d)/'isa.json',Path(d)/'baseline.json'
            state=deepcopy(self.state)
            state['telegram']['last_update_id']=123
            btc.save_state(bp,state);isa.save_state(ip,self.isa_state(complete=False),now_utc=self.monday)
            env={'GITHUB_EVENT_NAME':'schedule','QG_TRIGGER_SOURCE':'schedule',
                 'MANUAL_RESET_BTC_STATE':'false','MANUAL_RESET_ISA_STATE':'false',
                 'BTC_CACHE_KEY':'btc-fixed-six-state-123-1','ISA_CACHE_KEY':'isa-tiger-leverage-state-123-1'}
            with patch.object(guard,'BTC_PATH',bp),patch.object(guard,'ISA_PATH',ip),patch.object(guard,'BASELINE_PATH',baseline),patch.dict('os.environ',env,clear=False):
                guard.run('preflight');guard.run('finalize')
                state['telegram']['last_update_id']=122;btc.save_state(bp,state)
                with self.assertRaises(ValueError):guard.run('finalize')

    def test_external_recovery_cannot_reset_state(self):
        env={'GITHUB_EVENT_NAME':'workflow_dispatch','QG_TRIGGER_SOURCE':'cloudflare_watchdog',
             'QG_SERVICE_ONLY':'true','MANUAL_RESET_BTC_STATE':'true','MANUAL_RESET_ISA_STATE':'false'}
        with patch.dict('os.environ',env,clear=False):
            with self.assertRaises(ValueError):guard.run('preflight')


if __name__=='__main__':
    unittest.main()

import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import gw_notify as radar


def item(i=1, priority=100):
    return dict(title='2027寒假海外访学项目', url=f'https://internationaloffice.gdufs.edu.cn/info/1061/{i}.htm',
                source='国际项目·寒暑期项目', category='hsqxm', priority=priority, score=100,
                date=datetime.now(radar.CST).date().isoformat(), found=str(i), new_candidate=True)


def article(text='现面向本科生开放报名，资格与材料请查看附件。'):
    return dict(paras=[text], atts=[['通知.docx', 'https://example.org/notice.docx']], keys=[], deadline='', date='')


class DeliveryTests(unittest.TestCase):
    def test_new_then_same_then_changed(self):
        it, art = item(), article()
        radar.apply_article(it, art)
        first_hash = it.pop('pending_hash')
        it['delivered_hash'] = first_hash
        radar.apply_article(it, art)
        self.assertNotIn('pending_hash', it)
        updated = article('现面向本科生开放报名，报名时间延长，详情请查看附件。')
        radar.apply_article(it, updated)
        self.assertNotEqual(it['pending_hash'], first_hash)
        self.assertEqual(it['pending_reason'], 'updated')

    def test_first_run_and_old_archive_do_not_send(self):
        it = item()
        radar.apply_article(it, article(), first=True)
        self.assertNotIn('pending_hash', it)
        old = item()
        old['date'] = '2020-01-01'
        radar.apply_article(old, article())
        self.assertNotIn('pending_hash', old)

    def test_explicit_future_deadline_keeps_older_open_application(self):
        it, art = item(), article()
        it['date'] = '2025-01-01'
        art['deadline_iso'] = (datetime.now(radar.CST).date() + timedelta(days=30)).isoformat()
        radar.apply_article(it, art)
        self.assertIn('pending_hash', it)

    def test_old_notice_attachment_update_survives_next_run(self):
        it, art = item(), article()
        it['date'] = '2020-01-01'
        radar.apply_article(it, art)
        self.assertNotIn('pending_hash', it)
        art['atts'] = [['延期通知.docx', 'https://example.org/revised.docx']]
        radar.apply_article(it, art)
        self.assertEqual(it['pending_reason'], 'updated')
        fingerprint = it['pending_hash']
        radar.apply_article(it, art)
        self.assertEqual(it['pending_hash'], fingerprint)

    def test_static_overview_change_is_not_lost(self):
        it = item()
        it.update(category='ztqk', title='本科生国际化项目列表')
        radar.apply_article(it, article('项目总体介绍'))
        self.assertNotIn('pending_hash', it)
        radar.apply_article(it, article('项目总体介绍，资格条件有调整'))
        self.assertEqual(it['pending_reason'], 'updated')
        fingerprint = it['pending_hash']
        radar.apply_article(it, article('项目总体介绍，资格条件有调整'))
        self.assertEqual(it['pending_hash'], fingerprint)
        self.assertEqual(it['pending_reason'], 'updated')

    def test_limit_keeps_overflow_and_next_run_drains(self):
        items = [item(i) for i in range(10)]
        arts = {x['url']: article() for x in items}
        for it in items:
            radar.apply_article(it, arts[it['url']])
        st = {'items': {x['url']: x for x in items}}
        with patch.object(radar, 'send_mail', return_value=True) as send, patch.object(radar, 'save_state'), patch.object(radar.time, 'sleep'):
            self.assertEqual(radar.deliver_pending(st, arts), (8, []))
            self.assertEqual(len(radar.pending_items(st)), 2)
            self.assertEqual(radar.deliver_pending(st, arts), (2, []))
            self.assertEqual(send.call_count, 10)
            self.assertFalse(radar.pending_items(st))

    def test_failed_and_missing_credentials_preserve_pending(self):
        for response in (False, RuntimeError('SMTP down')):
            it, art = item(), article()
            radar.apply_article(it, art)
            st = {'items': {it['url']: it}}
            kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
            with patch.object(radar, 'send_mail', **kwargs), patch.object(radar, 'save_state'):
                sent, failures = radar.deliver_pending(st, {it['url']: art})
            self.assertEqual(sent, 0)
            self.assertTrue(failures)
            self.assertIn('pending_hash', it)
            self.assertNotIn('delivered_hash', it)

    def test_fetch_failure_skips_but_does_not_lose_pending(self):
        it = item()
        radar.apply_article(it, article())
        st = {'items': {it['url']: it}}
        with patch.object(radar, 'send_mail') as send:
            self.assertEqual(radar.deliver_pending(st, {}), (0, []))
        send.assert_not_called()
        self.assertIn('pending_hash', it)

    def test_priority_precedes_other_departments(self):
        regular, project = item(1, priority=0), item(2)
        regular['pending_hash'] = project['pending_hash'] = 'pending'
        st = {'items': {'a': regular, 'b': project}}
        self.assertEqual(radar.pending_items(st)[0], project)

    def test_year_comes_from_explicit_deadline(self):
        it, art = item(), article()
        art.update(deadline_iso='2027-03-01', deadline='2027年3月1日', deadline_time='14:00')
        date, clock, left = radar.deadline_info(it, art)
        self.assertEqual((date, clock), ('3月1日', '14:00'))
        self.assertEqual(left, (datetime(2027, 3, 1).date() - datetime.now(radar.CST).date()).days)

    def test_dry_run_does_not_write_or_send(self):
        source = radar.international.SOURCES[0]
        st = {'initialized': True, 'items': {}}
        row = item()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(radar, 'load_state', return_value=st), \
             patch.object(radar, 'collect_source', return_value=(source, [row], [])), \
             patch.object(radar, 'fetch_article', return_value=article()), \
             patch.object(radar, 'save_state') as save, \
             patch.object(radar, 'render_article') as render, \
             patch.object(radar, 'send_mail') as send, \
             patch.object(radar, 'DOCS', temp), \
             patch.object(radar.sys, 'argv', ['gw_notify.py', '--dry-run', '--international-only']):
            radar.main()
            save.assert_not_called()
            render.assert_not_called()
            send.assert_not_called()
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()

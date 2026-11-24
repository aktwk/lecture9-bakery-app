"""Final-build regression tests. Uses only temporary database copies."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import shutil
import threading
import unittest

import app


def run_week(conn, saturday_helper=False):
    days = []
    for _ in range(7):
        today = app.current_day(conn)
        app.mutate(conn, 'fetch', {})
        if saturday_helper and today == '2026-11-26':
            app.mutate(conn, 'helper', {'day': '2026-11-28', 'book': True})
        app.mutate(conn, 'batch', {})
        result = json.loads(conn.execute("SELECT value FROM meta WHERE key='last_batch'").fetchone()[0])
        days.append(result)
        for row in conn.execute('SELECT DISTINCT pickup FROM orders'):
            if app.used_minutes(conn, row[0]) > app.capacity(conn, row[0]):
                raise AssertionError('Overbooked pickup day: ' + row[0])
    requests = [dict(r) for r in conn.execute('SELECT * FROM requests ORDER BY id')]
    orders = [dict(r) for r in conn.execute('SELECT * FROM orders ORDER BY pickup,id')]
    revenue = sum(o['quantity']*app.PRODUCTS[o['product']]['price'] for o in orders if o['status'] in ('accepted','collected'))
    costs = 100*conn.execute('SELECT COUNT(*) FROM helpers').fetchone()[0]
    return {'scenario': 'Saturday helper booked Thursday' if saturday_helper else 'No helpers',
            'days': days, 'accepted_requests': sum(r['status']=='accepted' for r in requests),
            'incompatible_remaining': sum(r['status']=='pending' for r in requests),
            'sales_all_pickup_dates': revenue, 'helper_costs': costs,
            'balance_before_other_costs': revenue-costs, 'requests': requests, 'orders': orders,
            'capacity_by_day': [{'day':d,'hours':app.used_minutes(conn,d)/60,'capacity':app.capacity(conn,d)/60} for d in sorted({o['pickup'] for o in orders})]}


class FinalBakeryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.seed = Path(cls.temp.name)/'seed.db'
        app.initialize(cls.seed)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.path = Path(self.temp.name)/(self._testMethodName+'.db')
        shutil.copyfile(self.seed,self.path)
        self.conn = app.connect(self.path)

    def tearDown(self):
        self.conn.close()

    def row(self,name,kind='new'):
        return self.conn.execute('SELECT * FROM requests WHERE customer=? AND kind=?',(name,kind)).fetchone()

    def decision(self,name,kind='new',decision='accepted'):
        app.mutate(self.conn,'decision',{'id':self.row(name,kind)['id'],'decision':decision})

    def next_day(self):
        app.mutate(self.conn,'fetch',{})
        app.mutate(self.conn,'batch',{})

    def advance_to(self,day):
        while app.current_day(self.conn)<day:self.next_day()

    def test_initial_data_is_complete_but_messages_are_not_fetched(self):
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0],13)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],25)
        self.assertEqual(app.state(self.conn)['requests'],[])
        self.assertEqual(app.state(self.conn)['unfetched_today'],3)
        self.assertFalse(app.state(self.conn)['fetched_today'])

    def test_fetch_is_idempotent_and_hides_future_messages(self):
        app.mutate(self.conn,'fetch',{})
        app.mutate(self.conn,'fetch',{})
        self.assertEqual(len(app.state(self.conn)['requests']),3)
        self.assertEqual(self.conn.execute('SELECT SUM(fetched) FROM requests').fetchone()[0],3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0],13)

    def test_notice_boundary_and_cross_month_date(self):
        self.assertTrue(app.notice('2026-11-23','2026-11-25'))
        self.assertFalse(app.notice('2026-11-23','2026-11-24'))
        self.assertTrue(app.notice('2026-11-29','2026-12-01'))
        app.mutate(self.conn,'fetch',{})
        with self.assertRaises(app.InputError):self.decision('Okafor')
        self.decision('Jasmine')
        self.assertEqual(app.used_minutes(self.conn,'2026-11-25'),210)

    def test_unfetched_decisions_and_day_advance_are_blocked(self):
        with self.assertRaisesRegex(app.InputError,'Fetch'):self.decision('Jasmine')
        with self.assertRaisesRegex(app.InputError,'Fetch'):app.mutate(self.conn,'batch',{})
        with self.assertRaises(app.InputError):app.mutate(self.conn,'advance',{})
        self.assertEqual(app.current_day(self.conn),app.START)

    def test_monday_batch_accepts_valid_orders_and_closes_pickups(self):
        app.mutate(self.conn,'fetch',{})
        self.assertEqual(len(app.batch_preview(self.conn)['accepted']),2)
        app.mutate(self.conn,'batch',{})
        self.assertEqual(app.current_day(self.conn),'2026-11-24')
        self.assertEqual(self.row('Jasmine')['status'],'accepted')
        self.assertEqual(self.row('Patel')['status'],'accepted')
        self.assertEqual(self.row('Okafor')['status'],'pending')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM orders WHERE pickup='2026-11-23' AND status='collected'").fetchone()[0],2)
        self.assertEqual(app.used_minutes(self.conn,'2026-11-23'),240)
        self.assertFalse(app.state(self.conn)['fetched_today'])

    def test_fetch_cancellation_frees_capacity_without_new_order_acceptance(self):
        self.advance_to('2026-11-24')
        app.mutate(self.conn,'fetch',{})
        self.assertEqual(self.row('Brown','cancel')['status'],'accepted')
        self.assertEqual(app.used_minutes(self.conn,'2026-11-26'),300)
        self.assertEqual(self.row('Reyes')['status'],'pending')
        self.assertTrue(app.assess(self.conn,self.row('Reyes'))[0])

    def test_fetch_change_replaces_quantity_and_late_change_preserves_it(self):
        self.advance_to('2026-11-25')
        app.mutate(self.conn,'fetch',{})
        self.assertEqual(self.row('Moreau','change')['status'],'accepted')
        self.assertEqual(self.conn.execute("SELECT quantity FROM orders WHERE customer='Moreau'").fetchone()[0],6)
        self.next_day()
        app.mutate(self.conn,'fetch',{})
        self.assertFalse(app.assess(self.conn,self.row('Lam','change'))[0])
        self.assertEqual(self.conn.execute("SELECT quantity FROM orders WHERE customer='Lam'").fetchone()[0],2)

    def test_batch_checks_competing_requests_sequentially(self):
        app.mutate(self.conn,'fetch',{})
        for name in ['Big A','Big B']:
            app.mutate(self.conn,'request',{'customer':name,'kind':'new','product':'cake','quantity':3,'pickup':'2026-11-30'})
        preview=app.batch_preview(self.conn)
        self.assertIn(self.row('Big A')['id'],preview['accepted'])
        self.assertNotIn(self.row('Big B')['id'],preview['accepted'])
        app.mutate(self.conn,'batch',{})
        self.assertEqual(app.used_minutes(self.conn,'2026-11-30'),360)
        self.assertEqual(self.row('Big B')['status'],'pending')

    def test_helper_deadlines_limits_and_capacity_reclassification(self):
        with self.assertRaises(app.InputError):app.mutate(self.conn,'helper',{'day':'2026-11-24','book':True})
        self.advance_to('2026-11-26')
        app.mutate(self.conn,'fetch',{})
        self.assertFalse(app.assess(self.conn,self.row('Wilson'))[0])
        app.mutate(self.conn,'helper',{'day':'2026-11-28','book':True})
        self.assertTrue(app.assess(self.conn,self.row('Wilson'))[0])
        self.assertEqual(app.capacity(self.conn,'2026-11-28'),720)
        app.mutate(self.conn,'batch',{})
        self.assertEqual(app.used_minutes(self.conn,'2026-11-28'),720)
        with self.assertRaises(app.InputError):app.mutate(self.conn,'helper',{'day':'2026-11-28','book':False})

    def test_rejection_never_creates_an_order_or_accepts_twice(self):
        app.mutate(self.conn,'fetch',{})
        self.decision('Patel',decision='rejected')
        with self.assertRaises(app.InputError):self.decision('Patel')
        self.assertIsNone(self.conn.execute("SELECT 1 FROM orders WHERE customer='Patel'").fetchone())

    def test_unmatched_update_and_invalid_quantities(self):
        app.mutate(self.conn,'request',{'customer':'Unknown','kind':'cancel','pickup':'2026-11-27'})
        self.assertFalse(app.assess(self.conn,self.row('Unknown','cancel'))[0])
        for quantity in [0,-1,1.5,True,101]:
            with self.assertRaises(app.InputError):app.mutate(self.conn,'request',{'customer':'Bad','kind':'new','product':'cake','quantity':quantity,'pickup':'2026-11-27'})
        self.assertEqual(self.conn.execute('SELECT SUM(fetched) FROM requests WHERE received>?',(app.START,)).fetchone()[0],0)

    def test_persistence_and_reinitialization_keep_decisions(self):
        self.next_day()
        self.conn.commit()
        app.initialize(self.path)
        with closing(app.connect(self.path)) as reopened:
            self.assertEqual(app.current_day(reopened),'2026-11-24')
            self.assertEqual(reopened.execute('SELECT COUNT(*) FROM requests').fetchone()[0],25)
            self.assertEqual(reopened.execute("SELECT status FROM requests WHERE customer='Jasmine'").fetchone()[0],'accepted')

    def test_no_helper_full_week_scenario(self):
        report=run_week(self.conn)
        self.assertEqual(len(report['days']),7)
        self.assertEqual(app.current_day(self.conn),'2026-11-30')
        self.assertEqual(self.conn.execute('SELECT SUM(fetched) FROM requests').fetchone()[0],25)
        self.assertEqual(report['helper_costs'],0)
        self.assertEqual(self.row('Lee','cancel')['status'],'pending')
        self.assertEqual(self.conn.execute("SELECT status FROM orders WHERE customer='Lee'").fetchone()[0],'accepted')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM orders WHERE pickup<='2026-11-29' AND status='accepted'").fetchone()[0],0)

    def test_helper_full_week_scenario(self):
        report=run_week(self.conn,True)
        self.assertEqual(report['helper_costs'],100)
        self.assertEqual(report['balance_before_other_costs'],report['sales_all_pickup_dates']-100)
        self.assertEqual(self.row('Wilson')['status'],'accepted')

    def test_http_routes_atomic_failure_backup_and_origin_check(self):
        self.conn.commit()
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.make_handler(self.path))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        def post(action,data,origin=None):
            headers={'Content-Type':'application/json','X-Bakery-App':'1'}
            if origin:headers['Origin']=origin
            return urlopen(Request(url+'/api/'+action,data=json.dumps(data).encode(),headers=headers))
        try:
            with urlopen(url+'/api/state') as response:self.assertEqual(json.load(response)['requests'],[])
            with self.assertRaises(HTTPError) as error:post('batch',{})
            self.assertEqual(error.exception.code,400);error.exception.close()
            self.assertEqual(app.current_day(self.conn),app.START)
            with self.assertRaises(HTTPError) as error:post('fetch',{},'https://example.com')
            self.assertEqual(error.exception.code,403);error.exception.close()
            with post('fetch',{}) as response:self.assertEqual(len(json.load(response)['requests']),3)
            with urlopen(url+'/api/backup') as response:self.assertTrue(response.read().startswith(b'SQLite format 3'))
            with urlopen(url+'/api/export') as response:self.assertIn(b'Kim',response.read())
            for page in ['README.html','PLAN.html','TEST_RESULTS.html']:
                with urlopen(url+'/'+page) as response:self.assertEqual(response.status,200)
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main(verbosity=2)

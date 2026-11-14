from unittest import TestCase
from unittest.mock import patch

from deploy.feishu_sync import ticket_delta
from src.config import Settings
from src.qiyu.ticket import PAGE_SIZE, TicketError


class TestTicketDelta(TestCase):
    start = 1_794_500_000_001
    end = start + 60_000

    def test_reads_exact_windows_collects_all_pages_and_uses_latest_ticket(self):
        def search(*, offset, limit, bounds):
            self.assertEqual(limit, PAGE_SIZE)
            if bounds == {"start": self.start, "end": self.end}:
                rows = [{"id": identifier, "status": 5, "modifyTime": self.start, "staffId": 37}
                        for identifier in range(1, PAGE_SIZE + 2)]
                return len(rows), rows[offset:offset + limit]
            self.assertEqual(bounds, {"opStart": self.start, "opEnd": self.end})
            self.assertEqual(offset, 0)
            return 2, [{"id": "1", "status": 10, "modifyTime": self.end},
                       {"id": 100, "staffId": 41, "modifyTime": self.end}]

        with (patch.object(ticket_delta.TicketClient, "search", side_effect=search),
              patch.object(ticket_delta, "_linked_sessions", return_value={1: [{}]}) as linked):
            rows, coverage = ticket_delta.read(Settings(), self.start, self.end)

        results = {row["id"]: row for row in rows}
        linked.assert_called_once_with(Settings(), set(range(1, PAGE_SIZE + 2)) | {100})
        self.assertEqual(len(results), PAGE_SIZE + 2)
        self.assertEqual(results["1"]["status_code"], 10)
        self.assertEqual(results["1"]["submitter_id"], 37)
        self.assertEqual(results["1"]["linked_session_count"], 1)
        self.assertEqual(results["100"]["submitter_id"], 41)
        self.assertEqual(coverage["created_matches"], PAGE_SIZE + 1)
        self.assertEqual(coverage["updated_matches"], 2)

    def test_rejects_unstable_or_incomplete_pagination_before_projection(self):
        for last_page in ((3, [{"id": 2}]), (2, []), (2, [{"id": "1"}]),
                          (2, [{"id": 2}, {"id": 3}])):
            with (self.subTest(last_page=last_page),
                  patch.object(ticket_delta.TicketClient, "search", side_effect=[(2, [{"id": 1}]), (0, []),
                                                                                last_page]),
                  patch.object(ticket_delta, "_linked_sessions") as linked):
                with self.assertRaises(TicketError):
                    ticket_delta.read(Settings(), self.start, self.end)
                linked.assert_not_called()

    def test_empty_window_and_invalid_bounds(self):
        with patch.object(ticket_delta.TicketClient, "search", return_value=(0, [])) as search:
            self.assertEqual(ticket_delta.read(Settings(), self.start, self.start)[0], [])
            self.assertEqual(search.call_count, 2)
        with self.assertRaises(ValueError):
            ticket_delta.read(Settings(), self.end, self.start)

    def test_slice_reduces_both_branches_before_paginating_an_oversized_window(self):
        midpoint = (self.start + self.end) // 2
        requests = []

        def search(*, offset, limit, bounds):
            requests.append((offset, bounds))
            if "start" in bounds:
                return 0, []
            total = 201 if bounds["opEnd"] == self.end else 200
            return total, [{"id": offset + index + 1} for index in range(min(total - offset, limit))]

        with patch.object(ticket_delta.TicketClient, "search", side_effect=search):
            rows, coverage = ticket_delta.read_slice(Settings(), self.start, self.end)

        self.assertEqual(len(rows), 200)
        self.assertEqual(coverage["created_period"], [self.start, midpoint])
        self.assertEqual(coverage["updated_period"], [self.start, midpoint])
        self.assertEqual(requests[:2], [(0, {"start": self.start, "end": self.end}),
                                       (0, {"opStart": self.start, "opEnd": self.end})])
        self.assertEqual([offset for offset, bounds in requests[2:] if "opStart" in bounds], [0, 50, 100, 150])

    def test_slice_never_discards_tickets_that_share_one_millisecond(self):
        def search(*, offset, limit, bounds):
            if "opStart" in bounds:
                return 0, []
            self.assertEqual(bounds["start"], self.start)
            total = 201
            return total, [{"id": offset + index + 1} for index in range(min(total - offset, limit))]

        with patch.object(ticket_delta.TicketClient, "search", side_effect=search):
            rows, coverage = ticket_delta.read_slice(Settings(), self.start, self.start + 1)

        self.assertEqual(coverage["created_period"], [self.start, self.start])
        self.assertEqual(len(rows), 201)

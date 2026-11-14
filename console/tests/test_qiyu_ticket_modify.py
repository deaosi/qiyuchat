import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlsplit

from src.config import QiyuConfig
from src.qiyu import ticket


class TicketModifyTests(TestCase):
    def setUp(self):
        self.client = ticket.TicketClient(QiyuConfig(app_key="test-key", app_secret="test-secret"))

    def test_uses_native_modify_contract_with_holder_and_documented_types(self):
        with patch.object(ticket, "request", return_value={"code": 200, "message": True}) as request:
            self.client.modify(12, 37, change_type=1, value="工单标题")
            self.client.modify(12, 37, change_type=2, value="8")
            self.client.modify(12, 37, change_type=0, value="新需求", custom_id=200, field_id=100)
        payloads = []
        for call in request.call_args_list:
            method, url = call.args
            self.assertEqual(method, "POST")
            self.assertEqual(urlsplit(url).path, "/openapi/v2/ticket/modify")
            self.assertEqual(call.kwargs["retries"], 1)
            payloads.append(json.loads(call.kwargs["body"]))
        self.assertEqual(payloads, [
            {"ticketId": 12, "staffId": 37, "type": 1, "value": "工单标题", "customId": 0, "fieldId": 0},
            {"ticketId": 12, "staffId": 37, "type": 2, "value": "8", "customId": 0, "fieldId": 0},
            {"ticketId": 12, "staffId": 37, "type": 0, "value": "新需求", "customId": 200, "fieldId": 100},
        ])

    def test_lost_result_is_not_retried_and_needs_readback(self):
        with patch.object(ticket, "request", side_effect=TimeoutError) as request:
            with self.assertRaises(ticket.TicketModifyUnconfirmed):
                self.client.modify(12, 37, change_type=1, value="新标题")
        request.assert_called_once()

    def test_template_fields_validate_type_metadata(self):
        field = {"fieldId": 100, "name": "问题需求", "type": 0, "status": 0, "required": 1}
        with patch.object(ticket, "request", side_effect=[{"code": 200, "message": [field]},
                                                         {"code": 200, "message": [{**field, "type": "text"}]}]):
            self.assertEqual(self.client.template_fields(123), [field])
            with self.assertRaises(ticket.TicketError):
                self.client.template_fields(123)

    def test_explicit_rejection_needs_no_retry_and_invalid_changes_make_no_request(self):
        with patch.object(ticket, "request", return_value={"code": 200, "message": False}) as request:
            with self.assertRaises(ticket.TicketError) as caught:
                self.client.modify(12, 37, change_type=1, value="新标题")
            self.assertNotIsInstance(caught.exception, ticket.TicketModifyUnconfirmed)
            with self.assertRaises(ValueError):
                self.client.modify(12, 37, change_type=4, value="新状态")
            with self.assertRaises(ValueError):
                self.client.modify(12, 37, change_type=0, value="无字段 ID")
        request.assert_called_once()

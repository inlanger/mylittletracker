import pytest

from mylittletracker.providers.ctt import normalize_ctt_response
from mylittletracker.models import ShipmentStatus


def test_ctt_parser_from_sample_json():
    raw = {
        "data": {
            "shipping_history": {
                "item_code": "0082800082909720118884001",
                "events": [
                    {
                        "code": "0000",
                        "description": "Pendiente de recepción en CTT Express",
                        "type": "STATUS",
                        "event_date": "2025-09-06T14:05:03.399+00:00",
                        "detail": {
                            "event_courier_code": "null",
                            "item_event_datetime": "2025-09-06T14:05:03.399Z",
                        },
                    },
                    {
                        "code": "1000",
                        "description": "En tránsito",
                        "type": "STATUS",
                        "event_date": "2025-09-15T06:42:43.680+00:00",
                        "detail": {
                            "External_event_text": "null",
                            "event_courier_code": "null",
                            "item_event_datetime": "2025-09-16T02:13:30.127Z",
                            "item_event_text": "null",
                        },
                    },
                    {
                        "code": "1500",
                        "description": "Entrega hoy",
                        "type": "STATUS",
                        "event_date": "2025-09-16T06:59:40.102+00:00",
                        "detail": {
                            "External_event_text": "460U2683",
                            "event_courier_code": "460U2683",
                            "item_event_datetime": "2025-09-16T06:59:40.102Z",
                            "item_event_text": "460U2683",
                        },
                    },
                ],
                "item_length_declared": "0.0",
                "item_width_declared": "0.0",
                "item_height_declared": "0.0",
                "declared_weight": "1.45",
            },
            "shipping_code": "0082800082909720118884",
            "client_reference": "UK439746885YP",
            "reported_delivery_date": "2025-09-17",
            "committed_delivery_datetime": "2025-09-17",
            "delivery_date": "2025-09-17",
            "origin_province_name": "San Fernando de Henares",
            "destin_province_name": "Valencia",
            "origin_name": "San Fernando de Henares",
            "destin_name": "Valencia",
            "declared_weight": 1.45,
            "final_weight": 1.46,
            "shipping_type_code": "48P",
            "client_center_code": "4182300006",
            "client_code": "41823",
            "item_count": 1,
            "traffic_type_code": "TRANSIT_TRFT",
            "has_custom": False,
        },
        "error": None,
    }

    res = normalize_ctt_response(raw, "0082800082909720118884")
    assert res.provider == "ctt"
    assert len(res.shipments) == 1
    s = res.shipments[0]
    assert s.tracking_number == "0082800082909720118884"
    # Latest event is "Entrega hoy" -> out_for_delivery
    assert s.status == ShipmentStatus.OUT_FOR_DELIVERY
    assert len(s.events) == 3
    assert any("Pendiente" in e.status for e in s.events)
    assert any("En tránsito" in e.status for e in s.events)
    assert any("Entrega" in e.status for e in s.events)
    assert s.origin == "San Fernando de Henares"
    assert s.destination == "Valencia"

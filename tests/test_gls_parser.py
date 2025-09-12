import pytest

from mylittletracker.providers.gls import normalize_gls_parcels_response
from mylittletracker.models import ShipmentStatus


def test_gls_parser_200_without_errors():
    # Example adapted from Response200 -> 200WithoutErrors in the YAML
    raw = {
        "parcels": [
            {
                "requested": "36301917596",
                "unitno": "36301917596",
                "status": "PREADVICE",
                "statusDateTime": "2024-10-11T15:24:57+0200",
                "events": [
                    {
                        "code": "INTIAL.E48_DATA",
                        "city": "",
                        "postalCode": "",
                        "country": "DE",
                        "description": "The parcel was provided by the sender for collection by GLS.",
                        "eventDateTime": "2024-10-11T15:24:52+0200",
                    },
                    {
                        "code": "INTIAL.PREADVICE",
                        "city": "",
                        "postalCode": "",
                        "country": "DE",
                        "description": "The parcel data was entered into the GLS IT system; the parcel was not yet handed over to GLS.",
                        "eventDateTime": "2024-10-11T15:24:51+0200",
                    },
                ],
            }
        ]
    }

    res = normalize_gls_parcels_response(raw)
    assert res.provider == "gls"
    assert len(res.shipments) == 1
    s = res.shipments[0]
    assert s.tracking_number == "36301917596"
    assert s.status == ShipmentStatus.INFORMATION_RECEIVED  # PREADVICE maps to information_received
    assert len(s.events) == 2
    assert s.events[0].status.startswith("The parcel was provided")


def test_gls_parser_200_with_links_deliveredps():
    raw = {
        "parcels": [
            {
                "requested": "42249028960",
                "unitno": "42249028960",
                "status": "DELIVEREDPS",
                "statusDateTime": "2025-02-10T15:13:46+0100",
                "_links": {
                    "deliveryInfo": {
                        "href": "/tracking/deliveryinfo/parcelid/42249028960?{originaldestinationpostalcode}",
                        "templated": True,
                    }
                },
                "events": [
                    {
                        "code": "DELIVD.PARCELSHOP",
                        "city": "",
                        "postalCode": "",
                        "country": "",
                        "description": "The parcel has been delivered at the ParcelShop (see ParcelShop information).",
                        "eventDateTime": "2025-02-10T15:13:46+0100",
                    },
                    {
                        "code": "INTIAL.NORMAL",
                        "city": "",
                        "postalCode": "",
                        "country": "DE",
                        "description": "The parcel was handed over to GLS.",
                        "eventDateTime": "2025-02-10T15:11:12+0100",
                    },
                ],
            }
        ]
    }

    res = normalize_gls_parcels_response(raw)
    assert len(res.shipments) == 1
    s = res.shipments[0]
    assert s.status == ShipmentStatus.DELIVERED  # DELIVEREDPS -> delivered
    assert any("ParcelShop" in e.status for e in s.events)


def test_gls_parser_200_reference_two_parcels_intransit():
    raw = {
        "parcels": [
            {
                "requested": "747N6K",
                "unitno": "93835330599",
                "status": "INTRANSIT",
                "statusDateTime": "2024-09-18T06:50:22+0200",
                "events": [
                    {
                        "code": "INTIAL.NORMAL",
                        "city": "",
                        "postalCode": "",
                        "country": "IT",
                        "description": "The parcel was handed over to GLS.",
                        "eventDateTime": "2024-09-18T06:50:22+0200",
                    },
                ],
            },
            {
                "requested": "747N6K",
                "unitno": "93835330600",
                "status": "INTRANSIT",
                "statusDateTime": "2024-09-18T06:48:46+0200",
                "events": [
                    {
                        "code": "INTIAL.PREADVICE",
                        "city": "",
                        "postalCode": "",
                        "country": "IT",
                        "description": "The parcel data was entered into the GLS IT system; the parcel was not yet handed over to GLS.",
                        "eventDateTime": "2024-09-17T22:27:25+0200",
                    },
                ],
            },
        ]
    }

    res = normalize_gls_parcels_response(raw)
    assert len(res.shipments) == 2
    assert all(s.status == ShipmentStatus.IN_TRANSIT for s in res.shipments)


# Ecoscooting API Parameter Analysis

This document details the findings from reverse engineering the Ecoscooting tracking API, which uses Cainiao's logistics network.

## API Endpoint
```
POST https://de-link.cainiao.com/gateway/link.do
Content-Type: application/x-www-form-urlencoded
```

## Required Parameters

All parameters below are **required** for a successful API call:

### 1. `logistics_interface` (JSON string)
Contains the tracking request details:
```json
{
  "mailNo": "460070000042074578",
  "locale": "en_US",
  "role": "endUser"
}
```

### 2. `msg_type`
**Fixed value**: `CN_OVERSEA_LOGISTICS_INQUIRY_TRACKING`

### 3. `data_digest`
**Flexible**: Can be any string value (e.g., `suibianxie`, `test123`)

### 4. `logistic_provider_id`
**Fixed value**: `DISTRIBUTOR_30250031` (Ecoscooting's provider ID)

### 5. `to_code`
**Fixed value**: `CNL_EU` (Europe routing code)

## Parameter Requirements Analysis

### Required Parameters Test Results

| Parameter | Status | Error if Missing |
|-----------|--------|------------------|
| `logistics_interface` | ✅ Required | "request param api can not be null" |
| `msg_type` | ✅ Required | "request param api can not be null" |
| `data_digest` | ✅ Required | "request param DataDigest can not be null" |
| `logistic_provider_id` | ✅ Required | "request param fromCode can not be null" |
| `to_code` | ✅ Required | Service route fails without toCode |

### Flexible Parameters

| Parameter | Values Tested | Result |
|-----------|---------------|--------|
| `data_digest` | `suibianxie`, `test123`, `anything` | ✅ All work |
| `locale` | `en_US`, `es_ES` | ✅ Both work |
| `role` | `endUser`, `admin` | ✅ Both work |

### Fixed Parameters

| Parameter | Fixed Value | Alternative Tested | Result |
|-----------|-------------|-------------------|--------|
| `msg_type` | `CN_OVERSEA_LOGISTICS_INQUIRY_TRACKING` | N/A | ✅ Must be exact |
| `logistic_provider_id` | `DISTRIBUTOR_30250031` | N/A | ✅ Must be exact |
| `to_code` | `CNL_EU` | `ES` | ❌ "toCode ES is not authorized" |

## Complete Working Example

```bash
curl -X POST "https://de-link.cainiao.com/gateway/link.do" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "logistics_interface={\"mailNo\":\"460070000042074578\",\"locale\":\"en_US\",\"role\":\"endUser\"}" \
  -d "msg_type=CN_OVERSEA_LOGISTICS_INQUIRY_TRACKING" \
  -d "data_digest=suibianxie" \
  -d "logistic_provider_id=DISTRIBUTOR_30250031" \
  -d "to_code=CNL_EU"
```

## Response Format

Successful response returns JSON with:
- `success: "true"`
- `packageParam`: Package details (weight, destination, etc.)
- `statuses`: Array of tracking events with timestamps
- `popStationParam`: PUDO station details if applicable

## Implementation Notes

1. **`data_digest`** appears to be a placeholder/signature field that accepts any value
2. **`to_code=CNL_EU`** indicates this is specifically for Cainiao Europe logistics
3. **`logistic_provider_id=DISTRIBUTOR_30250031`** is Ecoscooting's specific provider ID
4. The API supports different locales for internationalization
5. Headers like `Origin` and `Referer` are not strictly required but recommended

## Error Codes Observed

- **S12**: Invalid system parameters (missing required fields)
- **S16**: Service route failed (wrong provider/routing)
- **S23**: Service not authorized for given toCode

---

*Analysis performed on 2025-01-17 by reverse engineering browser network requests*
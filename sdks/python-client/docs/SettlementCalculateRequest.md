# SettlementCalculateRequest


## Properties

Name | Type | Description | Notes
------------ | ------------- | ------------- | -------------
**contract_id** | **str** |  | 
**contract_version** | **int** |  | [optional] 
**idempotency_key** | **str** |  | [optional] 
**run_id** | **str** |  | 

## Example

```python
from datashell_client.models.settlement_calculate_request import SettlementCalculateRequest

# TODO update the JSON string below
json = "{}"
# create an instance of SettlementCalculateRequest from a JSON string
settlement_calculate_request_instance = SettlementCalculateRequest.from_json(json)
# print the JSON string representation of the object
print(SettlementCalculateRequest.to_json())

# convert the object into a dict
settlement_calculate_request_dict = settlement_calculate_request_instance.to_dict()
# create an instance of SettlementCalculateRequest from a dict
settlement_calculate_request_from_dict = SettlementCalculateRequest.from_dict(settlement_calculate_request_dict)
```
[[Back to Model list]](../README.md#documentation-for-models) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to README]](../README.md)



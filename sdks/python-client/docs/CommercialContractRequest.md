# CommercialContractRequest

Request body for registering a commercial contract (tenant-scoped).

## Properties

Name | Type | Description | Notes
------------ | ------------- | ------------- | -------------
**acceptance** | **Dict[str, object]** |  | [optional] 
**approved_by** | **List[str]** |  | [optional] 
**buyer_id** | **str** |  | 
**contract_id** | **str** |  | 
**contract_version** | **int** |  | [optional] [default to 1]
**currency** | **str** |  | [optional] [default to 'USD']
**description** | **str** |  | [optional] 
**effective_at** | **str** |  | 
**expires_at** | **str** |  | [optional] 
**feed_id** | **str** |  | 
**penalty** | **Dict[str, object]** |  | [optional] 
**pricing** | **Dict[str, object]** |  | 
**qualification** | **Dict[str, object]** |  | [optional] 
**schema_version** | **int** |  | [optional] [default to 1]
**seller_id** | **str** |  | 

## Example

```python
from datashell_client.models.commercial_contract_request import CommercialContractRequest

# TODO update the JSON string below
json = "{}"
# create an instance of CommercialContractRequest from a JSON string
commercial_contract_request_instance = CommercialContractRequest.from_json(json)
# print the JSON string representation of the object
print(CommercialContractRequest.to_json())

# convert the object into a dict
commercial_contract_request_dict = commercial_contract_request_instance.to_dict()
# create an instance of CommercialContractRequest from a dict
commercial_contract_request_from_dict = CommercialContractRequest.from_dict(commercial_contract_request_dict)
```
[[Back to Model list]](../README.md#documentation-for-models) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to README]](../README.md)



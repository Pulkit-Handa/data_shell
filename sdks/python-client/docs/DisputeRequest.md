# DisputeRequest


## Properties

Name | Type | Description | Notes
------------ | ------------- | ------------- | -------------
**disputing_party** | **str** |  | 
**evidence_references** | **List[str]** |  | [optional] 
**reason** | **str** |  | 
**supporting_explanation** | **str** |  | [optional] [default to '']

## Example

```python
from datashell_client.models.dispute_request import DisputeRequest

# TODO update the JSON string below
json = "{}"
# create an instance of DisputeRequest from a JSON string
dispute_request_instance = DisputeRequest.from_json(json)
# print the JSON string representation of the object
print(DisputeRequest.to_json())

# convert the object into a dict
dispute_request_dict = dispute_request_instance.to_dict()
# create an instance of DisputeRequest from a dict
dispute_request_from_dict = DisputeRequest.from_dict(dispute_request_dict)
```
[[Back to Model list]](../README.md#documentation-for-models) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to README]](../README.md)



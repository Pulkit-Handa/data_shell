# datashell_client.DefaultApi

All URIs are relative to *http://localhost*

Method | HTTP request | Description
------------- | ------------- | -------------
[**approve_settlement_api_v1_settlements_settlement_id_approvals_post**](DefaultApi.md#approve_settlement_api_v1_settlements_settlement_id_approvals_post) | **POST** /api/v1/settlements/{settlement_id}/approvals | Approve a settlement for billing
[**calculate_settlement_api_v1_settlements_calculate_post**](DefaultApi.md#calculate_settlement_api_v1_settlements_calculate_post) | **POST** /api/v1/settlements/calculate | Calculate (or idempotently replay) a settlement
[**create_commercial_contract_api_v1_commercial_contracts_post**](DefaultApi.md#create_commercial_contract_api_v1_commercial_contracts_post) | **POST** /api/v1/commercial-contracts | Register a commercial-contract version
[**create_managed_api_key_api_v1_admin_keys_post**](DefaultApi.md#create_managed_api_key_api_v1_admin_keys_post) | **POST** /api/v1/admin/keys | Issue a managed API credential
[**enforce_boundary_api_v1_enforce_post**](DefaultApi.md#enforce_boundary_api_v1_enforce_post) | **POST** /api/v1/enforce | Enforce the boundary for an upload
[**explain_settlement_api_v1_settlements_settlement_id_explanation_get**](DefaultApi.md#explain_settlement_api_v1_settlements_settlement_id_explanation_get) | **GET** /api/v1/settlements/{settlement_id}/explanation | Explain a settlement calculation
[**get_commercial_contract_api_v1_commercial_contracts_contract_id_get**](DefaultApi.md#get_commercial_contract_api_v1_commercial_contracts_contract_id_get) | **GET** /api/v1/commercial-contracts/{contract_id} | Read a commercial-contract version
[**get_contract_api_v1_contracts_name_get**](DefaultApi.md#get_contract_api_v1_contracts_name_get) | **GET** /api/v1/contracts/{name} | Read a data-contract version
[**get_ledger_run_api_v1_runs_run_id_get**](DefaultApi.md#get_ledger_run_api_v1_runs_run_id_get) | **GET** /api/v1/runs/{run_id} | Read one forensic run record
[**get_run_certificate_api_v1_runs_run_id_certificate_get**](DefaultApi.md#get_run_certificate_api_v1_runs_run_id_certificate_get) | **GET** /api/v1/runs/{run_id}/certificate | Fetch a run&#39;s audit certificate
[**get_settlement_api_v1_settlements_settlement_id_get**](DefaultApi.md#get_settlement_api_v1_settlements_settlement_id_get) | **GET** /api/v1/settlements/{settlement_id} | Read a settlement statement
[**healthcheck_health_get**](DefaultApi.md#healthcheck_health_get) | **GET** /health | Liveness probe
[**ledger_run_summary_api_v1_runs_summary_get**](DefaultApi.md#ledger_run_summary_api_v1_runs_summary_get) | **GET** /api/v1/runs/summary | Aggregate run health for a tenant
[**ledger_usage_api_v1_usage_get**](DefaultApi.md#ledger_usage_api_v1_usage_get) | **GET** /api/v1/usage | Metering totals for a tenant
[**list_commercial_contract_versions_api_v1_commercial_contracts_contract_id_versions_get**](DefaultApi.md#list_commercial_contract_versions_api_v1_commercial_contracts_contract_id_versions_get) | **GET** /api/v1/commercial-contracts/{contract_id}/versions | List commercial-contract versions
[**list_contracts_api_v1_contracts_get**](DefaultApi.md#list_contracts_api_v1_contracts_get) | **GET** /api/v1/contracts | List visible contract names
[**list_ledger_quarantine_api_v1_quarantine_get**](DefaultApi.md#list_ledger_quarantine_api_v1_quarantine_get) | **GET** /api/v1/quarantine | Search the quarantine index
[**list_ledger_runs_api_v1_runs_get**](DefaultApi.md#list_ledger_runs_api_v1_runs_get) | **GET** /api/v1/runs | List forensic run records
[**list_settlements_api_v1_settlements_get**](DefaultApi.md#list_settlements_api_v1_settlements_get) | **GET** /api/v1/settlements | List settlement statements
[**open_settlement_dispute_api_v1_settlements_settlement_id_disputes_post**](DefaultApi.md#open_settlement_dispute_api_v1_settlements_settlement_id_disputes_post) | **POST** /api/v1/settlements/{settlement_id}/disputes | Open a settlement dispute
[**prometheus_metrics_metrics_get**](DefaultApi.md#prometheus_metrics_metrics_get) | **GET** /metrics | Prometheus metrics scrape
[**readinesscheck_ready_get**](DefaultApi.md#readinesscheck_ready_get) | **GET** /ready | Readiness probe
[**register_contract_api_v1_contracts_post**](DefaultApi.md#register_contract_api_v1_contracts_post) | **POST** /api/v1/contracts | Register a data-contract version
[**revoke_managed_api_key_api_v1_admin_keys_key_id_delete**](DefaultApi.md#revoke_managed_api_key_api_v1_admin_keys_key_id_delete) | **DELETE** /api/v1/admin/keys/{key_id} | Revoke a managed API credential
[**serve_enterprise_console_get**](DefaultApi.md#serve_enterprise_console_get) | **GET** / | Serve the enterprise console
[**settlement_billing_preview_api_v1_settlements_settlement_id_billing_preview_post**](DefaultApi.md#settlement_billing_preview_api_v1_settlements_settlement_id_billing_preview_post) | **POST** /api/v1/settlements/{settlement_id}/billing-preview | Draft a billing instruction
[**settlement_preview_api_v1_settlement_preview_get**](DefaultApi.md#settlement_preview_api_v1_settlement_preview_get) | **GET** /api/v1/settlement/preview | Roll up the settlement portfolio for dashboards
[**verify_settlement_api_v1_settlements_settlement_id_verify_post**](DefaultApi.md#verify_settlement_api_v1_settlements_settlement_id_verify_post) | **POST** /api/v1/settlements/{settlement_id}/verify | Verify a settlement receipt


# **approve_settlement_api_v1_settlements_settlement_id_approvals_post**
> Dict[str, object] approve_settlement_api_v1_settlements_settlement_id_approvals_post(settlement_id, approval_request)

Approve a settlement for billing

Explicitly approve a settlement for billing (BILLING_MANAGE capability).

Approval records an audited transition; it never overrides the
contractual calculation and never marks an unverified settlement as
accepted.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.models.approval_request import ApprovalRequest
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    settlement_id = 'settlement_id_example' # str | 
    approval_request = datashell_client.ApprovalRequest() # ApprovalRequest | 

    try:
        # Approve a settlement for billing
        api_response = api_instance.approve_settlement_api_v1_settlements_settlement_id_approvals_post(settlement_id, approval_request)
        print("The response of DefaultApi->approve_settlement_api_v1_settlements_settlement_id_approvals_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->approve_settlement_api_v1_settlements_settlement_id_approvals_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **settlement_id** | **str**|  | 
 **approval_request** | [**ApprovalRequest**](ApprovalRequest.md)|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: application/json
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **calculate_settlement_api_v1_settlements_calculate_post**
> Dict[str, object] calculate_settlement_api_v1_settlements_calculate_post(settlement_calculate_request)

Calculate (or idempotently replay) a settlement

Calculate (or idempotently replay) the settlement for a completed run.

Verifies the delivery certificate before calculating; an invalid
certificate, failed reconciliation, or unresolvable contract fails closed
with 422 and never produces a payable statement.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.models.settlement_calculate_request import SettlementCalculateRequest
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    settlement_calculate_request = datashell_client.SettlementCalculateRequest() # SettlementCalculateRequest | 

    try:
        # Calculate (or idempotently replay) a settlement
        api_response = api_instance.calculate_settlement_api_v1_settlements_calculate_post(settlement_calculate_request)
        print("The response of DefaultApi->calculate_settlement_api_v1_settlements_calculate_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->calculate_settlement_api_v1_settlements_calculate_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **settlement_calculate_request** | [**SettlementCalculateRequest**](SettlementCalculateRequest.md)|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: application/json
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **create_commercial_contract_api_v1_commercial_contracts_post**
> Dict[str, object] create_commercial_contract_api_v1_commercial_contracts_post(commercial_contract_request)

Register a commercial-contract version

Register an immutable commercial contract version (tenant-scoped).

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.models.commercial_contract_request import CommercialContractRequest
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    commercial_contract_request = datashell_client.CommercialContractRequest() # CommercialContractRequest | 

    try:
        # Register a commercial-contract version
        api_response = api_instance.create_commercial_contract_api_v1_commercial_contracts_post(commercial_contract_request)
        print("The response of DefaultApi->create_commercial_contract_api_v1_commercial_contracts_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->create_commercial_contract_api_v1_commercial_contracts_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **commercial_contract_request** | [**CommercialContractRequest**](CommercialContractRequest.md)|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: application/json
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**201** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **create_managed_api_key_api_v1_admin_keys_post**
> Dict[str, object] create_managed_api_key_api_v1_admin_keys_post(create_api_key_request)

Issue a managed API credential

Create a credential and return its secret exactly once to an administrator.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.models.create_api_key_request import CreateAPIKeyRequest
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    create_api_key_request = datashell_client.CreateAPIKeyRequest() # CreateAPIKeyRequest | 

    try:
        # Issue a managed API credential
        api_response = api_instance.create_managed_api_key_api_v1_admin_keys_post(create_api_key_request)
        print("The response of DefaultApi->create_managed_api_key_api_v1_admin_keys_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->create_managed_api_key_api_v1_admin_keys_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **create_api_key_request** | [**CreateAPIKeyRequest**](CreateAPIKeyRequest.md)|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: application/json
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**201** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **enforce_boundary_api_v1_enforce_post**
> Dict[str, object] enforce_boundary_api_v1_enforce_post(contract_name, file, streaming=streaming)

Enforce the boundary for an upload

Submits a data payload across the boundary for contract enforcement.
Returns the reconciliation ledger and cryptographically signed audit certificate.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    contract_name = 'contract_name_example' # str | 
    file = 'file_example' # str | 
    streaming = False # bool |  (optional) (default to False)

    try:
        # Enforce the boundary for an upload
        api_response = api_instance.enforce_boundary_api_v1_enforce_post(contract_name, file, streaming=streaming)
        print("The response of DefaultApi->enforce_boundary_api_v1_enforce_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->enforce_boundary_api_v1_enforce_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **contract_name** | **str**|  | 
 **file** | **str**|  | 
 **streaming** | **bool**|  | [optional] [default to False]

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: multipart/form-data
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **explain_settlement_api_v1_settlements_settlement_id_explanation_get**
> Dict[str, object] explain_settlement_api_v1_settlements_settlement_id_explanation_get(settlement_id)

Explain a settlement calculation

Full calculation explanation: units, price, thresholds, rounding, evidence.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    settlement_id = 'settlement_id_example' # str | 

    try:
        # Explain a settlement calculation
        api_response = api_instance.explain_settlement_api_v1_settlements_settlement_id_explanation_get(settlement_id)
        print("The response of DefaultApi->explain_settlement_api_v1_settlements_settlement_id_explanation_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->explain_settlement_api_v1_settlements_settlement_id_explanation_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **settlement_id** | **str**|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **get_commercial_contract_api_v1_commercial_contracts_contract_id_get**
> Dict[str, object] get_commercial_contract_api_v1_commercial_contracts_contract_id_get(contract_id, version=version)

Read a commercial-contract version

Retrieve a commercial contract by id and optional exact version (tenant-scoped).

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    contract_id = 'contract_id_example' # str | 
    version = 56 # int |  (optional)

    try:
        # Read a commercial-contract version
        api_response = api_instance.get_commercial_contract_api_v1_commercial_contracts_contract_id_get(contract_id, version=version)
        print("The response of DefaultApi->get_commercial_contract_api_v1_commercial_contracts_contract_id_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->get_commercial_contract_api_v1_commercial_contracts_contract_id_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **contract_id** | **str**|  | 
 **version** | **int**|  | [optional] 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **get_contract_api_v1_contracts_name_get**
> Dict[str, object] get_contract_api_v1_contracts_name_get(name, version=version)

Read a data-contract version

Retrieves contract specification by name and optional point-in-time version within tenant scope.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    name = 'name_example' # str | 
    version = 'version_example' # str |  (optional)

    try:
        # Read a data-contract version
        api_response = api_instance.get_contract_api_v1_contracts_name_get(name, version=version)
        print("The response of DefaultApi->get_contract_api_v1_contracts_name_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->get_contract_api_v1_contracts_name_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **name** | **str**|  | 
 **version** | **str**|  | [optional] 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **get_ledger_run_api_v1_runs_run_id_get**
> Dict[str, object] get_ledger_run_api_v1_runs_run_id_get(run_id, tenant_id=tenant_id)

Read one forensic run record

Full forensic ledger record for a single run, tenant-scoped.

Administrators may target any tenant with ``tenant_id`` for oversight; every
other role is pinned to its own tenant and denied explicitly for any other.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    run_id = 'run_id_example' # str | 
    tenant_id = 'tenant_id_example' # str |  (optional)

    try:
        # Read one forensic run record
        api_response = api_instance.get_ledger_run_api_v1_runs_run_id_get(run_id, tenant_id=tenant_id)
        print("The response of DefaultApi->get_ledger_run_api_v1_runs_run_id_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->get_ledger_run_api_v1_runs_run_id_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **run_id** | **str**|  | 
 **tenant_id** | **str**|  | [optional] 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **get_run_certificate_api_v1_runs_run_id_certificate_get**
> Dict[str, object] get_run_certificate_api_v1_runs_run_id_certificate_get(run_id)

Fetch a run's audit certificate

Retrieves the immutable cryptographic audit certificate for a specific
run, with strict tenant ownership validation.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    run_id = 'run_id_example' # str | 

    try:
        # Fetch a run's audit certificate
        api_response = api_instance.get_run_certificate_api_v1_runs_run_id_certificate_get(run_id)
        print("The response of DefaultApi->get_run_certificate_api_v1_runs_run_id_certificate_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->get_run_certificate_api_v1_runs_run_id_certificate_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **run_id** | **str**|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **get_settlement_api_v1_settlements_settlement_id_get**
> Dict[str, object] get_settlement_api_v1_settlements_settlement_id_get(settlement_id)

Read a settlement statement

Retrieve one settlement statement (tenant-scoped).

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    settlement_id = 'settlement_id_example' # str | 

    try:
        # Read a settlement statement
        api_response = api_instance.get_settlement_api_v1_settlements_settlement_id_get(settlement_id)
        print("The response of DefaultApi->get_settlement_api_v1_settlements_settlement_id_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->get_settlement_api_v1_settlements_settlement_id_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **settlement_id** | **str**|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **healthcheck_health_get**
> Dict[str, object] healthcheck_health_get()

Liveness probe

Liveness probe for orchestrators, load balancers, and Kubernetes.

### Example


```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)


# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)

    try:
        # Liveness probe
        api_response = api_instance.healthcheck_health_get()
        print("The response of DefaultApi->healthcheck_health_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->healthcheck_health_get: %s\n" % e)
```



### Parameters

This endpoint does not need any parameter.

### Return type

**Dict[str, object]**

### Authorization

No authorization required

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **ledger_run_summary_api_v1_runs_summary_get**
> Dict[str, object] ledger_run_summary_api_v1_runs_summary_get(contract_name=contract_name, tenant_id=tenant_id)

Aggregate run health for a tenant

Aggregate delivery health for a tenant, optionally scoped to one contract.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    contract_name = 'contract_name_example' # str |  (optional)
    tenant_id = 'tenant_id_example' # str |  (optional)

    try:
        # Aggregate run health for a tenant
        api_response = api_instance.ledger_run_summary_api_v1_runs_summary_get(contract_name=contract_name, tenant_id=tenant_id)
        print("The response of DefaultApi->ledger_run_summary_api_v1_runs_summary_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->ledger_run_summary_api_v1_runs_summary_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **contract_name** | **str**|  | [optional] 
 **tenant_id** | **str**|  | [optional] 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **ledger_usage_api_v1_usage_get**
> Dict[str, object] ledger_usage_api_v1_usage_get(contract_name=contract_name, since=since, tenant_id=tenant_id)

Metering totals for a tenant

Metering totals for a tenant, optionally since an ISO-8601 timestamp.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    contract_name = 'contract_name_example' # str |  (optional)
    since = 'since_example' # str |  (optional)
    tenant_id = 'tenant_id_example' # str |  (optional)

    try:
        # Metering totals for a tenant
        api_response = api_instance.ledger_usage_api_v1_usage_get(contract_name=contract_name, since=since, tenant_id=tenant_id)
        print("The response of DefaultApi->ledger_usage_api_v1_usage_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->ledger_usage_api_v1_usage_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **contract_name** | **str**|  | [optional] 
 **since** | **str**|  | [optional] 
 **tenant_id** | **str**|  | [optional] 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **list_commercial_contract_versions_api_v1_commercial_contracts_contract_id_versions_get**
> Dict[str, object] list_commercial_contract_versions_api_v1_commercial_contracts_contract_id_versions_get(contract_id)

List commercial-contract versions

List all immutable versions of a commercial contract (tenant-scoped).

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    contract_id = 'contract_id_example' # str | 

    try:
        # List commercial-contract versions
        api_response = api_instance.list_commercial_contract_versions_api_v1_commercial_contracts_contract_id_versions_get(contract_id)
        print("The response of DefaultApi->list_commercial_contract_versions_api_v1_commercial_contracts_contract_id_versions_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->list_commercial_contract_versions_api_v1_commercial_contracts_contract_id_versions_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **contract_id** | **str**|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **list_contracts_api_v1_contracts_get**
> Dict[str, object] list_contracts_api_v1_contracts_get()

List visible contract names

Contract-name catalog visible to the caller's tenant (own + shared).

Powers client discovery (CLI completion, the console contract selector):
names and active versions only, never full contract payloads.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)

    try:
        # List visible contract names
        api_response = api_instance.list_contracts_api_v1_contracts_get()
        print("The response of DefaultApi->list_contracts_api_v1_contracts_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->list_contracts_api_v1_contracts_get: %s\n" % e)
```



### Parameters

This endpoint does not need any parameter.

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **list_ledger_quarantine_api_v1_quarantine_get**
> Dict[str, object] list_ledger_quarantine_api_v1_quarantine_get(contract_name=contract_name, run_id=run_id, kind=kind, reason=reason, tenant_id=tenant_id, limit=limit)

Search the quarantine index

Search the quarantine index by contract, run, kind, or dominant reason.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    contract_name = 'contract_name_example' # str |  (optional)
    run_id = 'run_id_example' # str |  (optional)
    kind = 'kind_example' # str |  (optional)
    reason = 'reason_example' # str |  (optional)
    tenant_id = 'tenant_id_example' # str |  (optional)
    limit = 100 # int |  (optional) (default to 100)

    try:
        # Search the quarantine index
        api_response = api_instance.list_ledger_quarantine_api_v1_quarantine_get(contract_name=contract_name, run_id=run_id, kind=kind, reason=reason, tenant_id=tenant_id, limit=limit)
        print("The response of DefaultApi->list_ledger_quarantine_api_v1_quarantine_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->list_ledger_quarantine_api_v1_quarantine_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **contract_name** | **str**|  | [optional] 
 **run_id** | **str**|  | [optional] 
 **kind** | **str**|  | [optional] 
 **reason** | **str**|  | [optional] 
 **tenant_id** | **str**|  | [optional] 
 **limit** | **int**|  | [optional] [default to 100]

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **list_ledger_runs_api_v1_runs_get**
> Dict[str, object] list_ledger_runs_api_v1_runs_get(contract_name=contract_name, status_filter=status_filter, tenant_id=tenant_id, limit=limit, offset=offset)

List forensic run records

Newest-first forensic run records for a tenant with optional filters.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    contract_name = 'contract_name_example' # str |  (optional)
    status_filter = 'status_filter_example' # str |  (optional)
    tenant_id = 'tenant_id_example' # str |  (optional)
    limit = 50 # int |  (optional) (default to 50)
    offset = 0 # int |  (optional) (default to 0)

    try:
        # List forensic run records
        api_response = api_instance.list_ledger_runs_api_v1_runs_get(contract_name=contract_name, status_filter=status_filter, tenant_id=tenant_id, limit=limit, offset=offset)
        print("The response of DefaultApi->list_ledger_runs_api_v1_runs_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->list_ledger_runs_api_v1_runs_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **contract_name** | **str**|  | [optional] 
 **status_filter** | **str**|  | [optional] 
 **tenant_id** | **str**|  | [optional] 
 **limit** | **int**|  | [optional] [default to 50]
 **offset** | **int**|  | [optional] [default to 0]

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **list_settlements_api_v1_settlements_get**
> Dict[str, object] list_settlements_api_v1_settlements_get(status=status, contract_id=contract_id, limit=limit, offset=offset)

List settlement statements

List settlement statements for the caller's tenant (paginated).

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    status = 'status_example' # str |  (optional)
    contract_id = 'contract_id_example' # str |  (optional)
    limit = 50 # int |  (optional) (default to 50)
    offset = 0 # int |  (optional) (default to 0)

    try:
        # List settlement statements
        api_response = api_instance.list_settlements_api_v1_settlements_get(status=status, contract_id=contract_id, limit=limit, offset=offset)
        print("The response of DefaultApi->list_settlements_api_v1_settlements_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->list_settlements_api_v1_settlements_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **status** | **str**|  | [optional] 
 **contract_id** | **str**|  | [optional] 
 **limit** | **int**|  | [optional] [default to 50]
 **offset** | **int**|  | [optional] [default to 0]

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **open_settlement_dispute_api_v1_settlements_settlement_id_disputes_post**
> Dict[str, object] open_settlement_dispute_api_v1_settlements_settlement_id_disputes_post(settlement_id, dispute_request)

Open a settlement dispute

Open a dispute; the settlement becomes DISPUTED and non-billable.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.models.dispute_request import DisputeRequest
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    settlement_id = 'settlement_id_example' # str | 
    dispute_request = datashell_client.DisputeRequest() # DisputeRequest | 

    try:
        # Open a settlement dispute
        api_response = api_instance.open_settlement_dispute_api_v1_settlements_settlement_id_disputes_post(settlement_id, dispute_request)
        print("The response of DefaultApi->open_settlement_dispute_api_v1_settlements_settlement_id_disputes_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->open_settlement_dispute_api_v1_settlements_settlement_id_disputes_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **settlement_id** | **str**|  | 
 **dispute_request** | [**DisputeRequest**](DisputeRequest.md)|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: application/json
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**201** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **prometheus_metrics_metrics_get**
> object prometheus_metrics_metrics_get()

Prometheus metrics scrape

Prometheus telemetry scrape endpoint (authenticated).

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)

    try:
        # Prometheus metrics scrape
        api_response = api_instance.prometheus_metrics_metrics_get()
        print("The response of DefaultApi->prometheus_metrics_metrics_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->prometheus_metrics_metrics_get: %s\n" % e)
```



### Parameters

This endpoint does not need any parameter.

### Return type

**object**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **readinesscheck_ready_get**
> Dict[str, object] readinesscheck_ready_get()

Readiness probe

Readiness probe: fail closed when required persistent directories are unavailable.

### Example


```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)


# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)

    try:
        # Readiness probe
        api_response = api_instance.readinesscheck_ready_get()
        print("The response of DefaultApi->readinesscheck_ready_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->readinesscheck_ready_get: %s\n" % e)
```



### Parameters

This endpoint does not need any parameter.

### Return type

**Dict[str, object]**

### Authorization

No authorization required

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **register_contract_api_v1_contracts_post**
> Dict[str, object] register_contract_api_v1_contracts_post(request_body)

Register a data-contract version

Registers and versions a Data Contract immutably, scoped to tenant catalog.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    request_body = None # Dict[str, object] | 

    try:
        # Register a data-contract version
        api_response = api_instance.register_contract_api_v1_contracts_post(request_body)
        print("The response of DefaultApi->register_contract_api_v1_contracts_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->register_contract_api_v1_contracts_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **request_body** | [**Dict[str, object]**](object.md)|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: application/json
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **revoke_managed_api_key_api_v1_admin_keys_key_id_delete**
> Dict[str, object] revoke_managed_api_key_api_v1_admin_keys_key_id_delete(key_id)

Revoke a managed API credential

Revoke a managed key by its fingerprint. Revocation is durable in production.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    key_id = 'key_id_example' # str | 

    try:
        # Revoke a managed API credential
        api_response = api_instance.revoke_managed_api_key_api_v1_admin_keys_key_id_delete(key_id)
        print("The response of DefaultApi->revoke_managed_api_key_api_v1_admin_keys_key_id_delete:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->revoke_managed_api_key_api_v1_admin_keys_key_id_delete: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **key_id** | **str**|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **serve_enterprise_console_get**
> str serve_enterprise_console_get()

Serve the enterprise console

Serve the live enterprise console for this gateway deployment.

### Example


```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)


# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)

    try:
        # Serve the enterprise console
        api_response = api_instance.serve_enterprise_console_get()
        print("The response of DefaultApi->serve_enterprise_console_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->serve_enterprise_console_get: %s\n" % e)
```



### Parameters

This endpoint does not need any parameter.

### Return type

**str**

### Authorization

No authorization required

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: text/html

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **settlement_billing_preview_api_v1_settlements_settlement_id_billing_preview_post**
> Dict[str, object] settlement_billing_preview_api_v1_settlements_settlement_id_billing_preview_post(settlement_id)

Draft a billing instruction

Create or replay a draft billing instruction via the configured provider.

Read-only against the settlement; the external action is a *draft* invoice.
Requires the Idempotency-Key header for the state-changing external call.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    settlement_id = 'settlement_id_example' # str | 

    try:
        # Draft a billing instruction
        api_response = api_instance.settlement_billing_preview_api_v1_settlements_settlement_id_billing_preview_post(settlement_id)
        print("The response of DefaultApi->settlement_billing_preview_api_v1_settlements_settlement_id_billing_preview_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->settlement_billing_preview_api_v1_settlements_settlement_id_billing_preview_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **settlement_id** | **str**|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **settlement_preview_api_v1_settlement_preview_get**
> Dict[str, object] settlement_preview_api_v1_settlement_preview_get(limit=limit)

Roll up the settlement portfolio for dashboards

Dashboard-facing aggregate of the tenant's settlement portfolio.

A bounded read-only roll-up of the durable settlement ledger: totals by
currency, status distribution, and the newest statements. It performs no
calculation, transition, or billing action.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    limit = 50 # int |  (optional) (default to 50)

    try:
        # Roll up the settlement portfolio for dashboards
        api_response = api_instance.settlement_preview_api_v1_settlement_preview_get(limit=limit)
        print("The response of DefaultApi->settlement_preview_api_v1_settlement_preview_get:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->settlement_preview_api_v1_settlement_preview_get: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **limit** | **int**|  | [optional] [default to 50]

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

# **verify_settlement_api_v1_settlements_settlement_id_verify_post**
> Dict[str, object] verify_settlement_api_v1_settlements_settlement_id_verify_post(settlement_id)

Verify a settlement receipt

Verify a settlement's signed receipt against its delivery certificate.

### Example

* Api Key Authentication (APIKeyHeader):

```python
import datashell_client
from datashell_client.rest import ApiException
from pprint import pprint

# Defining the host is optional and defaults to http://localhost
# See configuration.py for a list of all supported configuration parameters.
configuration = datashell_client.Configuration(
    host = "http://localhost"
)

# The client must configure the authentication and authorization parameters
# in accordance with the API server security policy.
# Examples for each auth method are provided below, use the example that
# satisfies your auth use case.

# Configure API key authorization: APIKeyHeader
configuration.api_key['APIKeyHeader'] = os.environ["API_KEY"]

# Uncomment below to setup prefix (e.g. Bearer) for API key, if needed
# configuration.api_key_prefix['APIKeyHeader'] = 'Bearer'

# Enter a context with an instance of the API client
with datashell_client.ApiClient(configuration) as api_client:
    # Create an instance of the API class
    api_instance = datashell_client.DefaultApi(api_client)
    settlement_id = 'settlement_id_example' # str | 

    try:
        # Verify a settlement receipt
        api_response = api_instance.verify_settlement_api_v1_settlements_settlement_id_verify_post(settlement_id)
        print("The response of DefaultApi->verify_settlement_api_v1_settlements_settlement_id_verify_post:\n")
        pprint(api_response)
    except Exception as e:
        print("Exception when calling DefaultApi->verify_settlement_api_v1_settlements_settlement_id_verify_post: %s\n" % e)
```



### Parameters


Name | Type | Description  | Notes
------------- | ------------- | ------------- | -------------
 **settlement_id** | **str**|  | 

### Return type

**Dict[str, object]**

### Authorization

[APIKeyHeader](../README.md#APIKeyHeader)

### HTTP request headers

 - **Content-Type**: Not defined
 - **Accept**: application/json

### HTTP response details

| Status code | Description | Response headers |
|-------------|-------------|------------------|
**200** | Successful Response |  -  |
**422** | Validation Error |  -  |

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


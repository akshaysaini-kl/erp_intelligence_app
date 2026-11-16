app_name = "erp_intelligence"
app_title = "ERP Intelligence"
app_publisher = "Auriga IT"
app_description = "Metadata-aware ERP query and aggregation tools for Frappe"
app_email = "praveen.kumawat@aurigait.com"
app_license = "mit"

assistant_tools = [
    "erp_intelligence.tools.describe_doctype.DescribeDocType",
    "erp_intelligence.tools.query_records.QueryRecords",
    "erp_intelligence.tools.aggregate_records.AggregateRecords",
    "erp_intelligence.tools.intelligence_request.IntelligenceRequest",
]

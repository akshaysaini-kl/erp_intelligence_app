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
    "erp_intelligence.tools.evaluate_business_rules.EvaluateBusinessRules",
]

# evaluate_business_rules ships disabled: these hooks create its disabled FAC Tool
# Configuration row before FAC's own migrate sync could create an enabled one.
before_migrate = "erp_intelligence.utils.fac_tool_configuration.before_migrate"
after_install = "erp_intelligence.utils.fac_tool_configuration.after_install"

-- SoloLedger Phase 10: BigQuery Property Graph DDL & Native GoogleSQL GQL Queries
-- Placeholders {{PROJECT_ID}} and {{DATASET_ID}} are rendered safely via bq_graph_exporter.py.

-- =============================================================================
-- 1. Node Base Tables (5 Entities)
-- =============================================================================
CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.merchants` (
    biz_reg_no STRING NOT NULL,
    merchant_name STRING NOT NULL,
    tax_type_code STRING,
    biz_status STRING,
    is_pg_provider BOOL,
    default_account_code STRING,
    PRIMARY KEY (biz_reg_no) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.card_transactions` (
    tx_id INT64 NOT NULL,
    card_last4 STRING,
    approval_no STRING,
    transacted_at STRING,
    tax_year INT64,
    year_month STRING,
    raw_merchant_name STRING,
    biz_reg_no STRING,
    total_amount INT64,
    supply_value INT64,
    vat_amount INT64,
    account_code STRING,
    account_name STRING,
    vat_deduction_type STRING,
    non_deduct_reason STRING,
    is_vehicle_expense BOOL,
    counterparty_info STRING,
    reconcile_status STRING,
    PRIMARY KEY (tx_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.order_line_items` (
    item_id STRING NOT NULL,
    tx_id INT64 NOT NULL,
    item_name STRING NOT NULL,
    normalized_keyword STRING,
    qty INT64,
    unit_price INT64,
    line_amount INT64,
    PRIMARY KEY (item_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.account_categories` (
    account_code STRING NOT NULL,
    account_name STRING NOT NULL,
    cost_nature STRING,
    is_construction_cost BOOL,
    PRIMARY KEY (account_code) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.construction_sites` (
    site_id STRING NOT NULL,
    site_name STRING NOT NULL,
    client_hint STRING,
    PRIMARY KEY (site_id) NOT ENFORCED
);

-- =============================================================================
-- 2. Edge Base Tables (4 Relationships)
-- =============================================================================
CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.edge_purchased_at` (
    edge_id STRING NOT NULL,
    tx_id INT64 NOT NULL,
    biz_reg_no STRING NOT NULL,
    total_amount INT64,
    transacted_at STRING,
    PRIMARY KEY (edge_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.edge_contains_item` (
    edge_id STRING NOT NULL,
    tx_id INT64 NOT NULL,
    item_id STRING NOT NULL,
    qty INT64,
    line_amount INT64,
    PRIMARY KEY (edge_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.edge_classified_as` (
    edge_id STRING NOT NULL,
    tx_id INT64 NOT NULL,
    account_code STRING NOT NULL,
    vat_deduction_type STRING,
    PRIMARY KEY (edge_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.edge_attributed_to` (
    edge_id STRING NOT NULL,
    tx_id INT64 NOT NULL,
    site_id STRING NOT NULL,
    attributed_amount INT64,
    PRIMARY KEY (edge_id) NOT ENFORCED
);

-- =============================================================================
-- 3. BigQuery Property Graph Definition
-- =============================================================================
CREATE PROPERTY GRAPH IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.sololedger_tax_graph`
  NODE TABLES (
    `{{PROJECT_ID}}.{{DATASET_ID}}.merchants` AS Merchant
      KEY (biz_reg_no)
      LABEL Merchant
      PROPERTIES (biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider, default_account_code),
    `{{PROJECT_ID}}.{{DATASET_ID}}.card_transactions` AS CardTransaction
      KEY (tx_id)
      LABEL CardTransaction
      PROPERTIES (
        tx_id, card_last4, transacted_at, tax_year, year_month, raw_merchant_name,
        total_amount, supply_value, vat_amount, account_code, account_name,
        vat_deduction_type, non_deduct_reason, is_vehicle_expense, counterparty_info,
        MEASURE(SUM(total_amount)) AS sum_total_spend,
        MEASURE(SUM(vat_amount)) AS sum_vat_amount
      ),
    `{{PROJECT_ID}}.{{DATASET_ID}}.order_line_items` AS OrderLineItem
      KEY (item_id)
      LABEL OrderLineItem
      PROPERTIES (item_id, tx_id, item_name, normalized_keyword, qty, unit_price, line_amount),
    `{{PROJECT_ID}}.{{DATASET_ID}}.account_categories` AS AccountCategory
      KEY (account_code)
      LABEL AccountCategory
      PROPERTIES (account_code, account_name, cost_nature, is_construction_cost),
    `{{PROJECT_ID}}.{{DATASET_ID}}.construction_sites` AS ConstructionSite
      KEY (site_id)
      LABEL ConstructionSite
      PROPERTIES (site_id, site_name, client_hint)
  )
  EDGE TABLES (
    `{{PROJECT_ID}}.{{DATASET_ID}}.edge_purchased_at` AS PurchasedAt
      KEY (edge_id)
      SOURCE KEY (tx_id) REFERENCES CardTransaction (tx_id)
      DESTINATION KEY (biz_reg_no) REFERENCES Merchant (biz_reg_no)
      LABEL PURCHASED_AT
      PROPERTIES (edge_id, total_amount, transacted_at),
    `{{PROJECT_ID}}.{{DATASET_ID}}.edge_contains_item` AS ContainsItem
      KEY (edge_id)
      SOURCE KEY (tx_id) REFERENCES CardTransaction (tx_id)
      DESTINATION KEY (item_id) REFERENCES OrderLineItem (item_id)
      LABEL CONTAINS_ITEM
      PROPERTIES (edge_id, qty, line_amount),
    `{{PROJECT_ID}}.{{DATASET_ID}}.edge_classified_as` AS ClassifiedAs
      KEY (edge_id)
      SOURCE KEY (tx_id) REFERENCES CardTransaction (tx_id)
      DESTINATION KEY (account_code) REFERENCES AccountCategory (account_code)
      LABEL CLASSIFIED_AS
      PROPERTIES (edge_id, vat_deduction_type),
    `{{PROJECT_ID}}.{{DATASET_ID}}.edge_attributed_to` AS AttributedTo
      KEY (edge_id)
      SOURCE KEY (tx_id) REFERENCES CardTransaction (tx_id)
      DESTINATION KEY (site_id) REFERENCES ConstructionSite (site_id)
      LABEL ATTRIBUTED_TO
      PROPERTIES (edge_id, attributed_amount)
  );

-- =============================================================================
-- 4. Native GoogleSQL GQL Risk & Cost Analytics Queries
-- =============================================================================

-- @gql_query: mixed_welfare_entertainment_merchants
GRAPH `{{PROJECT_ID}}.{{DATASET_ID}}.sololedger_tax_graph`
MATCH (tx:CardTransaction)-[:PURCHASED_AT]->(m:Merchant)
FILTER EXISTS {
  GRAPH `{{PROJECT_ID}}.{{DATASET_ID}}.sololedger_tax_graph`
  MATCH (tx1:CardTransaction {account_code: '811'})-[:PURCHASED_AT]->(m)
} AND EXISTS {
  GRAPH `{{PROJECT_ID}}.{{DATASET_ID}}.sololedger_tax_graph`
  MATCH (tx2:CardTransaction {account_code: '813'})-[:PURCHASED_AT]->(m)
}
RETURN m.biz_reg_no AS biz_reg_no, m.merchant_name AS merchant_name, COUNT(tx) AS tx_count, SUM(tx.total_amount) AS total_spend
ORDER BY total_spend DESC
-- @end_gql_query

-- @gql_query: item_keyword_account_recommendation
GRAPH `{{PROJECT_ID}}.{{DATASET_ID}}.sololedger_tax_graph`
MATCH (li:OrderLineItem)<-[:CONTAINS_ITEM]-(tx:CardTransaction)-[:CLASSIFIED_AS]->(ac:AccountCategory)
FILTER ac.account_code <> '999'
RETURN li.normalized_keyword AS normalized_keyword, ac.account_code AS account_code, ac.account_name AS account_name, COUNT(tx) AS support_tx_count, SUM(tx.total_amount) AS total_spend
ORDER BY support_tx_count DESC, total_spend DESC
LIMIT 100
-- @end_gql_query

-- @gql_query: construction_site_cost_rollup
GRAPH `{{PROJECT_ID}}.{{DATASET_ID}}.sololedger_tax_graph`
MATCH (cs:ConstructionSite)<-[:ATTRIBUTED_TO]-(tx:CardTransaction)-[:CLASSIFIED_AS]->(ac:AccountCategory)
FILTER ac.is_construction_cost = TRUE
RETURN cs.site_id AS site_id, cs.site_name AS site_name, ac.account_code AS account_code, ac.account_name AS account_name, COUNT(tx) AS tx_count, SUM(tx.total_amount) AS total_site_cost
ORDER BY total_site_cost DESC
-- @end_gql_query

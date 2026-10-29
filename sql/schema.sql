-- SoloLedger: 1-Person Corporation Expense & Tax Evidence Ledger Schema
-- Compatible with SQLite 3.x (and adaptable to PostgreSQL)

-- 1. Merchants and NTS Tax Status Cache Table
CREATE TABLE IF NOT EXISTS merchants (
    biz_reg_no VARCHAR(10) PRIMARY KEY, -- 10-digit business registration number without hyphens
    merchant_name VARCHAR(100) NOT NULL,
    tax_type_code VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN', -- GENERAL, SIMPLIFIED, EXEMPT, UNREGISTERED, UNKNOWN
    biz_status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE', -- ACTIVE, SUSPENDED, CLOSED
    is_pg_provider BOOLEAN NOT NULL DEFAULT FALSE, -- True for Coupang, NaverPay, AliExpress, etc.
    default_account_code VARCHAR(20), -- Cached accounting code for recurring visits
    nts_verified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. Corporate Card Transaction & Tax Journal Ledger Table
CREATE TABLE IF NOT EXISTS card_transactions (
    tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_last4 CHAR(4) NOT NULL,
    approval_no VARCHAR(20),
    transacted_at TIMESTAMP NOT NULL,
    raw_merchant_name VARCHAR(100) NOT NULL,
    biz_reg_no VARCHAR(10),
    total_amount INTEGER NOT NULL,
    supply_value INTEGER NOT NULL,
    vat_amount INTEGER NOT NULL,
    account_code VARCHAR(20) NOT NULL DEFAULT '999', -- 999: Unclassified temporary code
    account_name VARCHAR(50) NOT NULL DEFAULT '미분류',
    vat_deduction_type VARCHAR(20) NOT NULL DEFAULT 'PENDING', -- DEDUCTIBLE, NON_DEDUCTIBLE, EXEMPT, PENDING
    non_deduct_reason VARCHAR(50), -- ENTERTAINMENT, PASSENGER_CAR, SIMPLIFIED_TAX, PERSONAL_USE
    is_vehicle_expense BOOLEAN NOT NULL DEFAULT FALSE, -- Tracks 15M KRW annual corporate car limit
    counterparty_info VARCHAR(200), -- Attendee names for meetings or item details for PG purchases
    reconcile_status VARCHAR(20) NOT NULL DEFAULT 'SMS_ONLY', -- SMS_ONLY, OCR_MATCHED, HOMETAX_VERIFIED, CANCELED
    exported_year_month CHAR(6), -- Format: YYYYMM when exported for tax accountant
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (biz_reg_no) REFERENCES merchants(biz_reg_no)
);

-- Indexes for fast reconciliation and tax period querying
CREATE INDEX IF NOT EXISTS idx_tx_transacted_at ON card_transactions(transacted_at);
CREATE INDEX IF NOT EXISTS idx_tx_approval_no ON card_transactions(approval_no);
CREATE INDEX IF NOT EXISTS idx_tx_reconcile_status ON card_transactions(reconcile_status);
CREATE INDEX IF NOT EXISTS idx_tx_exported_ym ON card_transactions(exported_year_month);

-- 3. Receipt OCR and Evidence Archive Metadata Table
CREATE TABLE IF NOT EXISTS receipt_ocr_metadata (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id INTEGER,
    source_channel VARCHAR(20) NOT NULL DEFAULT 'TELEGRAM_BOT', -- TELEGRAM_BOT, WEB_UPLOAD, EMAIL_FORWARD
    original_file_path VARCHAR(255) NOT NULL,
    archived_file_name VARCHAR(255) NOT NULL,
    file_hash_sha256 CHAR(64) NOT NULL UNIQUE, -- Prevents duplicate upload of the same receipt image
    ocr_biz_reg_no VARCHAR(10),
    ocr_total_amount INTEGER,
    line_items_json TEXT, -- JSON array of purchased items extracted by Vision LLM
    raw_ocr_response TEXT, -- Full JSON payload from Vision LLM for debugging
    ocr_confidence NUMERIC(4, 2),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tx_id) REFERENCES card_transactions(tx_id) ON DELETE SET NULL
);

-- 4. Automated Tax Classification Rules Table
CREATE TABLE IF NOT EXISTS tax_classification_rules (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    priority INTEGER NOT NULL DEFAULT 100, -- Lower number evaluates first
    match_field VARCHAR(30) NOT NULL, -- MERCHANT_NAME, LINE_ITEM, BIZ_REG_NO
    match_pattern VARCHAR(100) NOT NULL, -- Regex pattern for matching
    target_account_code VARCHAR(20) NOT NULL,
    target_account_name VARCHAR(50) NOT NULL,
    force_non_deductible BOOLEAN NOT NULL DEFAULT FALSE,
    non_deduct_reason VARCHAR(50),
    set_vehicle_flag BOOLEAN NOT NULL DEFAULT FALSE,
    require_memo_prompt BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(priority, match_field, match_pattern)
);

-- Initial Seed Data for 1-Person Corporation Tax Rules
DELETE FROM tax_classification_rules
WHERE priority IN (10, 20, 40)
  AND match_pattern IN (
    '(쿠팡|네이버페이|알리익스프레스|이니시스|토스페이먼츠|카카오페이)',
    '(주유소|에너지|스피드메이트|오토큐|하이패스|주차)',
    '(컴포즈|매머드|브루다|스타벅스|맥도날드|롯데리아|맘스터치|샐러디)'
  );
INSERT OR IGNORE INTO tax_classification_rules
(priority, match_field, match_pattern, target_account_code, target_account_name, force_non_deductible, non_deduct_reason, set_vehicle_flag, require_memo_prompt)
VALUES
-- Rule 1: E-commerce & PG providers require item specification
(10, 'MERCHANT_NAME', '(쿠팡|네이버페이|알리익스프레스|이니시스|토스페이먼츠|카카오페이|PAYCO|페이코)', '830', '소모품비', 0, NULL, 0, 1),
-- Rule 2: Gas stations and auto maintenance -> Vehicle Expense + Non-deductible VAT (Passenger car)
(20, 'MERCHANT_NAME', '(주유소|에너지|오일|석유|칼텍스|스피드메이트|오토큐|하이패스|주차)', '822', '차량유지비', 1, 'PASSENGER_CAR', 1, 0),
-- Rule 3: SaaS, AI subscriptions, Cloud servers -> Service Fees
(30, 'MERCHANT_NAME', '(GITHUB|OPENAI|GOOGLE|AWS|클라우드)', '831', '지급수수료', 0, NULL, 0, 0),
-- Rule 4: Cafes, Restaurants, Convenience stores -> Prompt user to choose between Welfare vs. Business Promotion
(40, 'MERCHANT_NAME', '(컴포즈|매머드|브루다|스타벅스|맥도날드|롯데리아|맘스터치|샐러디|카페|식당|푸드|통닭|수산|씨유|GS25|코리아세븐)', '811', '복리후생비', 0, NULL, 0, 1);

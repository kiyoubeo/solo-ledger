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

-- Initial Seed Data for 1-Person Corporation Tax Rules (Firefly III-style expanded ruleset)
DELETE FROM tax_classification_rules
WHERE priority IN (10, 20, 30, 35, 38, 40);
INSERT OR IGNORE INTO tax_classification_rules
(priority, match_field, match_pattern, target_account_code, target_account_name, force_non_deductible, non_deduct_reason, set_vehicle_flag, require_memo_prompt)
VALUES
-- Rule 1: E-commerce & PG providers require item specification
(10, 'MERCHANT_NAME', '(쿠팡|네이버페이|네이버파이낸셜|알리익스프레스|이니시스|토스페이먼츠|카카오페이|주식회사카카|PAYCO|페이코|스마일페이|십일번가|11번가|지마켓|옥션|무신사)', '830', '소모품비', 0, NULL, 0, 1),
-- Rule 2: Gas stations, auto maintenance, parking, toll -> Vehicle Expense + Non-deductible VAT (Passenger car)
(20, 'MERCHANT_NAME', '(주유|에너지|에너|정유|오일|석유|칼텍스|스피드메이트|오토큐|하이패스|주차|파크|정비|모터스|타이어|세차|카쉐어링|쏘카|피플카)', '822', '차량유지비', 1, 'PASSENGER_CAR', 1, 0),
-- Rule 3: SaaS, AI subscriptions, Cloud, Telecom, Transit, Utilities -> Service Fees
(30, 'MERCHANT_NAME', '(GITHUB|OPENAI|GOOGLE|구글페이먼트|AWS|클라우드|웨이브|넷플릭스|유튜브|한국전력|도시가스|코레일|한국철도|SRT|택시|이비카드|티머니|우체국|펄어비스|블리자드|STEAMGAMES)', '831', '지급수수료', 0, NULL, 0, 0),
-- Rule 4: Cafes, Restaurants, Convenience stores, Bakeries, Pharmacies -> Welfare Expense
(40, 'MERCHANT_NAME', '(이마트24|컴포즈|컴파스커피|매머드|브루다|스타벅스|투썸|이디야|메가엠지씨|메가커피|빽다방|더벤티|더리터|드롭탑|폴바셋|할리스|파스쿠찌|맥도날드|롯데리아|맘스터치|버거|샐러디|서브웨이|가마로강정|카페|커피|베이커리|성심당|파리바게뜨|뚜레쥬르|식당|푸드|에프앤|통닭|치킨|피자|수산|회관|면옥|국밥|갈비|삼겹|김밥|분식|휴게소|쿠킹|우아한형제|주식회사우아|요기요|씨유|GS25|WE25|세븐일레븐|코리아세|미니스톱|동아대학교|약국|의원|병원|치과|한의원|헤어|피부)', '811', '복리후생비', 0, NULL, 0, 1);

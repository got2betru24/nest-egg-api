-- =============================================================================
-- NestEgg - Retirement Planning Calculator
-- 01_schema.sql - Tables, constraints, indexes
-- =============================================================================

SET FOREIGN_KEY_CHECKS = 0;

-- -----------------------------------------------------------------------------
-- Reference / Seed Tables (populated by 02_seed.sql)
-- -----------------------------------------------------------------------------

-- Tax brackets by year and filing status
CREATE TABLE IF NOT EXISTS tax_brackets (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tax_year        SMALLINT UNSIGNED NOT NULL,
    filing_status   ENUM('single', 'married_filing_jointly', 'married_filing_separately', 'head_of_household') NOT NULL,
    rate            DECIMAL(6,4) NOT NULL COMMENT 'e.g. 0.22 for 22%',
    income_min      DECIMAL(14,2) NOT NULL,
    income_max      DECIMAL(14,2) NULL COMMENT 'NULL = no upper limit (top bracket)',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_bracket (tax_year, filing_status, rate, income_min)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Standard deduction by year and filing status
CREATE TABLE IF NOT EXISTS standard_deductions (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tax_year        SMALLINT UNSIGNED NOT NULL,
    filing_status   ENUM('single', 'married_filing_jointly', 'married_filing_separately', 'head_of_household') NOT NULL,
    amount          DECIMAL(12,2) NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_deduction (tax_year, filing_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- IRS contribution limits by year and account type
CREATE TABLE IF NOT EXISTS contribution_limits (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tax_year        SMALLINT UNSIGNED NOT NULL,
    account_type    ENUM('401k', 'roth_401k', 'ira', 'roth_ira') NOT NULL,
    limit_type      ENUM('standard', 'catchup') NOT NULL COMMENT 'catchup applies age 50+',
    amount          DECIMAL(10,2) NOT NULL,
    catchup_age     TINYINT UNSIGNED NULL COMMENT 'Age at which catchup begins (typically 50)',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_limit (tax_year, account_type, limit_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Social Security bend points by year (for PIA calculation)
CREATE TABLE IF NOT EXISTS ss_bend_points (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    benefit_year    SMALLINT UNSIGNED NOT NULL COMMENT 'Year of eligibility (age 62)',
    bend_point_1    DECIMAL(10,2) NOT NULL COMMENT 'First bend point in AIME',
    bend_point_2    DECIMAL(10,2) NOT NULL COMMENT 'Second bend point in AIME',
    factor_below_1  DECIMAL(6,4) NOT NULL DEFAULT 0.9000 COMMENT 'PIA factor below bend 1',
    factor_1_to_2   DECIMAL(6,4) NOT NULL DEFAULT 0.3200 COMMENT 'PIA factor between bends',
    factor_above_2  DECIMAL(6,4) NOT NULL DEFAULT 0.1500 COMMENT 'PIA factor above bend 2',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_bend (benefit_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Social Security COLA adjustments by year
CREATE TABLE IF NOT EXISTS ss_cola (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    cola_year       SMALLINT UNSIGNED NOT NULL,
    rate            DECIMAL(6,4) NOT NULL COMMENT 'e.g. 0.087 for 8.7%',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_cola (cola_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- AWI (Average Wage Index) by year, used for SS earnings indexing
CREATE TABLE IF NOT EXISTS ss_awi (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    awi_year        SMALLINT UNSIGNED NOT NULL,
    awi_value       DECIMAL(12,2) NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_awi (awi_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Social Security full retirement age by birth year
CREATE TABLE IF NOT EXISTS ss_fra (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    birth_year_min  SMALLINT UNSIGNED NOT NULL,
    birth_year_max  SMALLINT UNSIGNED NOT NULL,
    fra_years       TINYINT UNSIGNED NOT NULL COMMENT 'Full retirement age in years',
    fra_months      TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Additional months',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------------------------
-- User / Scenario Tables
-- -----------------------------------------------------------------------------

-- Top-level scenario (saved plan)
CREATE TABLE IF NOT EXISTS scenarios (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(120) NOT NULL,
    description     TEXT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    is_active       TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Personal profile for each person in the scenario (supports couple)
CREATE TABLE IF NOT EXISTS persons (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scenario_id     INT UNSIGNED NOT NULL,
    role            ENUM('primary', 'spouse') NOT NULL,
    birth_year      SMALLINT UNSIGNED NOT NULL,
    birth_month     TINYINT UNSIGNED NOT NULL DEFAULT 1,
    planned_retirement_age  TINYINT UNSIGNED NOT NULL DEFAULT 55,
    CONSTRAINT fk_persons_scenario FOREIGN KEY (scenario_id)
        REFERENCES scenarios(id) ON DELETE CASCADE,
    UNIQUE KEY uq_person_role (scenario_id, role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Global assumptions for a scenario
CREATE TABLE IF NOT EXISTS scenario_assumptions (
    id                      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scenario_id             INT UNSIGNED NOT NULL,
    inflation_rate          DECIMAL(6,4) NOT NULL DEFAULT 0.0300 COMMENT 'Annual inflation assumption',
    plan_to_age             TINYINT UNSIGNED NOT NULL DEFAULT 90 COMMENT 'Age to plan through (longevity)',
    filing_status           ENUM('married_filing_jointly') NOT NULL DEFAULT 'married_filing_jointly',
    current_income          DECIMAL(14,2) NOT NULL DEFAULT 0,
    desired_retirement_income DECIMAL(14,2) NOT NULL DEFAULT 0 COMMENT 'In today dollars',
    healthcare_annual_cost  DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT 'Pre-Medicare bridge cost per year',
    enable_catchup_contributions TINYINT(1) NOT NULL DEFAULT 0,
    enable_roth_ladder      TINYINT(1) NOT NULL DEFAULT 0,
    return_scenario         ENUM('conservative', 'base', 'optimistic') NOT NULL DEFAULT 'base',
    CONSTRAINT fk_assumptions_scenario FOREIGN KEY (scenario_id)
        REFERENCES scenarios(id) ON DELETE CASCADE,
    UNIQUE KEY uq_assumptions (scenario_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Account definitions and current balances
CREATE TABLE IF NOT EXISTS accounts (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scenario_id     INT UNSIGNED NOT NULL,
    account_type    ENUM('hysa', 'brokerage', 'roth_ira', 'traditional_401k', 'roth_401k') NOT NULL,
    label           VARCHAR(80) NULL COMMENT 'Optional custom label',
    current_balance DECIMAL(16,2) NOT NULL DEFAULT 0,
    -- Return assumptions per scenario (conservative / base / optimistic)
    return_conservative DECIMAL(6,4) NOT NULL DEFAULT 0.0400,
    return_base         DECIMAL(6,4) NOT NULL DEFAULT 0.0700,
    return_optimistic   DECIMAL(6,4) NOT NULL DEFAULT 0.1000,
    CONSTRAINT fk_accounts_scenario FOREIGN KEY (scenario_id)
        REFERENCES scenarios(id) ON DELETE CASCADE,
    UNIQUE KEY uq_account_type (scenario_id, account_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Annual contribution plan per account
CREATE TABLE IF NOT EXISTS contributions (
    id                      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    account_id              INT UNSIGNED NOT NULL,
    annual_amount           DECIMAL(12,2) NOT NULL DEFAULT 0,
    employer_match_amount   DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT '401k employer match',
    enforce_irs_limits      TINYINT(1) NOT NULL DEFAULT 1,
    solve_mode              ENUM('fixed', 'solve_for') NOT NULL DEFAULT 'fixed'
                            COMMENT 'fixed=user sets amount; solve_for=optimizer calculates needed',
    CONSTRAINT fk_contributions_account FOREIGN KEY (account_id)
        REFERENCES accounts(id) ON DELETE CASCADE,
    UNIQUE KEY uq_contributions_account (account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Social Security earnings history per person
CREATE TABLE IF NOT EXISTS ss_earnings (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    person_id       INT UNSIGNED NOT NULL,
    earn_year       SMALLINT UNSIGNED NOT NULL,
    earnings        DECIMAL(14,2) NOT NULL DEFAULT 0,
    CONSTRAINT fk_ss_earnings_person FOREIGN KEY (person_id)
        REFERENCES persons(id) ON DELETE CASCADE,
    UNIQUE KEY uq_earnings (person_id, earn_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Social Security claiming strategy per person
CREATE TABLE IF NOT EXISTS ss_claiming (
    id                      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    person_id               INT UNSIGNED NOT NULL,
    claim_age_years         TINYINT UNSIGNED NOT NULL DEFAULT 67,
    claim_age_months        TINYINT UNSIGNED NOT NULL DEFAULT 0,
    use_spousal_benefit     TINYINT(1) NOT NULL DEFAULT 0,
    spousal_benefit_pct     DECIMAL(5,4) NOT NULL DEFAULT 0.5000 COMMENT 'Fraction of primary PIA (typically 0.50)',
    CONSTRAINT fk_claiming_person FOREIGN KEY (person_id)
        REFERENCES persons(id) ON DELETE CASCADE,
    UNIQUE KEY uq_claiming (person_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Roth conversion ladder schedule (optimizer output, tunable)
CREATE TABLE IF NOT EXISTS roth_conversions (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scenario_id     INT UNSIGNED NOT NULL,
    plan_year       SMALLINT UNSIGNED NOT NULL COMMENT 'Calendar year of conversion',
    amount          DECIMAL(14,2) NOT NULL DEFAULT 0,
    source_account  ENUM('traditional_401k', 'hysa', 'brokerage') NOT NULL DEFAULT 'traditional_401k',
    is_optimizer_suggested TINYINT(1) NOT NULL DEFAULT 1,
    CONSTRAINT fk_conversions_scenario FOREIGN KEY (scenario_id)
        REFERENCES scenarios(id) ON DELETE CASCADE,
    UNIQUE KEY uq_conversion_year (scenario_id, plan_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Cached projection results (invalidated on input change)
CREATE TABLE IF NOT EXISTS projection_cache (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scenario_id     INT UNSIGNED NOT NULL,
    return_scenario ENUM('conservative', 'base', 'optimistic') NOT NULL DEFAULT 'base',
    plan_year       SMALLINT UNSIGNED NOT NULL,
    age_primary     TINYINT UNSIGNED NOT NULL,
    age_spouse      TINYINT UNSIGNED NULL,
    -- Account balances at end of year
    bal_hysa                DECIMAL(16,2) NOT NULL DEFAULT 0,
    bal_brokerage           DECIMAL(16,2) NOT NULL DEFAULT 0,
    bal_roth_ira            DECIMAL(16,2) NOT NULL DEFAULT 0,
    bal_traditional_401k    DECIMAL(16,2) NOT NULL DEFAULT 0,
    bal_roth_401k           DECIMAL(16,2) NOT NULL DEFAULT 0,
    bal_total_pretax        DECIMAL(16,2) NOT NULL DEFAULT 0,
    bal_total_posttax       DECIMAL(16,2) NOT NULL DEFAULT 0,
    bal_total               DECIMAL(16,2) NOT NULL DEFAULT 0,
    -- Income and tax in this year
    gross_income            DECIMAL(14,2) NOT NULL DEFAULT 0,
    ss_income_primary       DECIMAL(14,2) NOT NULL DEFAULT 0,
    ss_income_spouse        DECIMAL(14,2) NOT NULL DEFAULT 0,
    taxable_income          DECIMAL(14,2) NOT NULL DEFAULT 0,
    tax_owed                DECIMAL(14,2) NOT NULL DEFAULT 0,
    effective_tax_rate      DECIMAL(6,4) NOT NULL DEFAULT 0,
    -- Withdrawal amounts
    withdrawal_hysa         DECIMAL(14,2) NOT NULL DEFAULT 0,
    withdrawal_brokerage    DECIMAL(14,2) NOT NULL DEFAULT 0,
    withdrawal_roth_ira     DECIMAL(14,2) NOT NULL DEFAULT 0,
    withdrawal_401k         DECIMAL(14,2) NOT NULL DEFAULT 0,
    withdrawal_roth_401k    DECIMAL(14,2) NOT NULL DEFAULT 0,
    roth_conversion_amount  DECIMAL(14,2) NOT NULL DEFAULT 0,
    -- Contributions (accumulation phase only)
    contrib_401k            DECIMAL(14,2) NOT NULL DEFAULT 0,
    contrib_roth_401k       DECIMAL(14,2) NOT NULL DEFAULT 0,
    contrib_roth_ira        DECIMAL(14,2) NOT NULL DEFAULT 0,
    computed_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_cache_scenario FOREIGN KEY (scenario_id)
        REFERENCES scenarios(id) ON DELETE CASCADE,
    UNIQUE KEY uq_cache (scenario_id, return_scenario, plan_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;

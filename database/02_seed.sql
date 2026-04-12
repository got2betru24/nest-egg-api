-- =============================================================================
-- NestEgg - Retirement Planning Calculator
-- 02_seed.sql - Reference data: tax brackets, SS bend points, contribution limits
-- =============================================================================
-- All dollar amounts are nominal for the given year.
-- Update annually as IRS and SSA publish new figures.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 2025 Federal Tax Brackets - Married Filing Jointly
-- Source: IRS Rev. Proc. 2024-40
-- -----------------------------------------------------------------------------
INSERT INTO tax_brackets (tax_year, filing_status, rate, income_min, income_max) VALUES
-- 10%
(2025, 'married_filing_jointly', 0.10,      0.00,   23850.00),
-- 12%
(2025, 'married_filing_jointly', 0.12,  23850.00,   96950.00),
-- 22%
(2025, 'married_filing_jointly', 0.22,  96950.00,  206700.00),
-- 24%
(2025, 'married_filing_jointly', 0.24, 206700.00,  394600.00),
-- 32%
(2025, 'married_filing_jointly', 0.32, 394600.00,  501050.00),
-- 35%
(2025, 'married_filing_jointly', 0.35, 501050.00,  751600.00),
-- 37%
(2025, 'married_filing_jointly', 0.37, 751600.00,       NULL)
ON DUPLICATE KEY UPDATE rate=VALUES(rate), income_max=VALUES(income_max);

-- Single filer brackets (for future use / general tool use)
INSERT INTO tax_brackets (tax_year, filing_status, rate, income_min, income_max) VALUES
(2025, 'single', 0.10,      0.00,  11925.00),
(2025, 'single', 0.12,  11925.00,  48475.00),
(2025, 'single', 0.22,  48475.00, 103350.00),
(2025, 'single', 0.24, 103350.00, 197300.00),
(2025, 'single', 0.32, 197300.00, 250525.00),
(2025, 'single', 0.35, 250525.00, 626350.00),
(2025, 'single', 0.37, 626350.00,       NULL)
ON DUPLICATE KEY UPDATE rate=VALUES(rate), income_max=VALUES(income_max);

-- Long-term capital gains brackets MFJ 2025 (brokerage account withdrawals)
-- Stored as a comment for reference; modeled in engine via separate logic
-- 0%:   $0 – $96,700
-- 15%:  $96,700 – $600,050
-- 20%:  $600,050+
-- Net Investment Income Tax (NIIT): 3.8% on investment income above $250k MFJ

-- -----------------------------------------------------------------------------
-- 2025 Standard Deductions
-- Source: IRS Rev. Proc. 2024-40
-- -----------------------------------------------------------------------------
INSERT INTO standard_deductions (tax_year, filing_status, amount) VALUES
(2025, 'married_filing_jointly',        30000.00),
(2025, 'single',                        15000.00),
(2025, 'married_filing_separately',     15000.00),
(2025, 'head_of_household',             22500.00)
ON DUPLICATE KEY UPDATE amount=VALUES(amount);

-- -----------------------------------------------------------------------------
-- 2025 IRS Contribution Limits
-- Source: IRS Notice 2024-80
-- -----------------------------------------------------------------------------
INSERT INTO contribution_limits (tax_year, account_type, limit_type, amount, catchup_age) VALUES
-- 401(k) / Roth 401(k)
(2025, '401k',      'standard', 23500.00, NULL),
(2025, '401k',      'catchup',   7500.00,   50),   -- ages 50-59 and 64+
(2025, 'roth_401k', 'standard', 23500.00, NULL),
(2025, 'roth_401k', 'catchup',   7500.00,   50),
-- SECURE 2.0: ages 60-63 get enhanced catch-up ($11,250 total catch-up)
-- Modeled in engine: if age 60-63, catchup = 11250 instead of 7500

-- Traditional IRA / Roth IRA (combined limit; doubled for married couple filing jointly each)
(2025, 'ira',      'standard', 7000.00, NULL),
(2025, 'ira',      'catchup',  1000.00,   50),
(2025, 'roth_ira', 'standard', 7000.00, NULL),
(2025, 'roth_ira', 'catchup',  1000.00,   50)
ON DUPLICATE KEY UPDATE amount=VALUES(amount);

-- -----------------------------------------------------------------------------
-- 2026 Federal Tax Brackets
-- Includes OBBBA permanent bracket structures and inflation adjustments.
-- -----------------------------------------------------------------------------
INSERT INTO tax_brackets (tax_year, filing_status, rate, income_min, income_max) VALUES
-- Married Filing Jointly
(2026, 'married_filing_jointly', 0.10,      0.00,   24800.00),
(2026, 'married_filing_jointly', 0.12,  24800.00,  100800.00),
(2026, 'married_filing_jointly', 0.22, 100800.00,  211400.00),
(2026, 'married_filing_jointly', 0.24, 211400.00,  403550.00),
(2026, 'married_filing_jointly', 0.32, 403550.00,  512450.00),
(2026, 'married_filing_jointly', 0.35, 512450.00,  768700.00),
(2026, 'married_filing_jointly', 0.37, 768700.00,       NULL),
-- Single
(2026, 'single', 0.10,      0.00,   12400.00),
(2026, 'single', 0.12,  12400.00,   50400.00),
(2026, 'single', 0.22,  50400.00,  105700.00),
(2026, 'single', 0.24, 105700.00,  201775.00),
(2026, 'single', 0.32, 201775.00,  256225.00),
(2026, 'single', 0.35, 256225.00,  640600.00),
(2026, 'single', 0.37, 640600.00,       NULL)
ON DUPLICATE KEY UPDATE rate=VALUES(rate), income_max=VALUES(income_max);

-- -----------------------------------------------------------------------------
-- 2026 Standard Deductions
-- -----------------------------------------------------------------------------
INSERT INTO standard_deductions (tax_year, filing_status, amount) VALUES
(2026, 'married_filing_jointly',        32200.00),
(2026, 'single',                        16100.00),
(2026, 'married_filing_separately',     16100.00),
(2026, 'head_of_household',             24150.00)
ON DUPLICATE KEY UPDATE amount=VALUES(amount);

-- -----------------------------------------------------------------------------
-- 2026 IRS Contribution Limits
-- Reflected SECURE 2.0 "Super Catch-Up" for ages 60-63.
-- -----------------------------------------------------------------------------
INSERT INTO contribution_limits (tax_year, account_type, limit_type, amount, catchup_age) VALUES
-- 401(k) / Roth 401(k)
(2026, '401k',      'standard', 24500.00, NULL),
(2026, '401k',      'catchup',   8000.00,   50),   -- Standard catch-up (50-59, 64+)
(2026, '401k',      'catchup',  11250.00,   60),   -- "Super" catch-up (60-63)
(2026, 'roth_401k', 'standard', 24500.00, NULL),
(2026, 'roth_401k', 'catchup',   8000.00,   50),
(2026, 'roth_401k', 'catchup',  11250.00,   60),
-- IRA / Roth IRA
(2026, 'ira',       'standard',  7500.00, NULL),
(2026, 'ira',       'catchup',   1100.00,   50),
(2026, 'roth_ira',  'standard',  7500.00, NULL),
(2026, 'roth_ira',  'catchup',   1100.00,   50)
ON DUPLICATE KEY UPDATE amount=VALUES(amount);


-- Benefit year = year person turns 62 (first year of eligibility)
-- Source: SSA.gov - https://www.ssa.gov/oact/cola/bendpoints.html
-- -----------------------------------------------------------------------------
INSERT INTO ss_bend_points (benefit_year, bend_point_1, bend_point_2, factor_below_1, factor_1_to_2, factor_above_2) VALUES
(2010,  761.00,  4586.00, 0.9, 0.32, 0.15),
(2011,  749.00,  4517.00, 0.9, 0.32, 0.15),
(2012,  767.00,  4624.00, 0.9, 0.32, 0.15),
(2013,  791.00,  4768.00, 0.9, 0.32, 0.15),
(2014,  816.00,  4917.00, 0.9, 0.32, 0.15),
(2015,  826.00,  4980.00, 0.9, 0.32, 0.15),
(2016,  856.00,  5157.00, 0.9, 0.32, 0.15),
(2017,  885.00,  5336.00, 0.9, 0.32, 0.15),
(2018,  895.00,  5397.00, 0.9, 0.32, 0.15),
(2019,  926.00,  5583.00, 0.9, 0.32, 0.15),
(2020,  960.00,  5785.00, 0.9, 0.32, 0.15),
(2021,  996.00,  6002.00, 0.9, 0.32, 0.15),
(2022, 1024.00,  6172.00, 0.9, 0.32, 0.15),
(2023, 1115.00,  6721.00, 0.9, 0.32, 0.15),
(2024, 1174.00,  7078.00, 0.9, 0.32, 0.15),
(2025, 1226.00,  7391.00, 0.9, 0.32, 0.15)
(2026, 1286.00,  7749.00, 0.9, 0.32, 0.15)
ON DUPLICATE KEY UPDATE bend_point_1=VALUES(bend_point_1), bend_point_2=VALUES(bend_point_2);

-- -----------------------------------------------------------------------------
-- Social Security COLA History
-- Source: SSA.gov - https://www.ssa.gov/oact/cola/colaseries.html
-- -----------------------------------------------------------------------------
INSERT INTO ss_cola (cola_year, rate) VALUES
(2010, 0.0000),
(2011, 0.0000),
(2012, 0.0360),
(2013, 0.0170),
(2014, 0.0150),
(2015, 0.0170),
(2016, 0.0000),
(2017, 0.0030),
(2018, 0.0200),
(2019, 0.0280),
(2020, 0.0160),
(2021, 0.0130),
(2022, 0.0590),
(2023, 0.0870),
(2024, 0.0320),
(2025, 0.0250),
(2026, 0.0280)
ON DUPLICATE KEY UPDATE rate=VALUES(rate);

-- -----------------------------------------------------------------------------
-- Social Security Average Wage Index (AWI)
-- Used to index historical earnings to the year the worker turns 60.
-- Source: SSA.gov - https://www.ssa.gov/oact/cola/AWI.html
-- -----------------------------------------------------------------------------
INSERT INTO ss_awi (awi_year, awi_value) VALUES
(1985, 16822.51),
(1986, 17321.82),
(1987, 18426.51),
(1988, 19334.04),
(1989, 20099.55),
(1990, 21027.98),
(1991, 21811.60),
(1992, 22935.42),
(1993, 23132.67),
(1994, 23753.53),
(1995, 24705.66),
(1996, 25913.90),
(1997, 27426.00),
(1998, 28861.44),
(1999, 30469.84),
(2000, 32154.82),
(2001, 32921.92),
(2002, 33252.09),
(2003, 34064.95),
(2004, 35648.55),
(2005, 36952.94),
(2006, 38651.41),
(2007, 40405.48),
(2008, 41334.97),
(2009, 40711.61),
(2010, 41673.83),
(2011, 42979.61),
(2012, 44321.67),
(2013, 44888.16),
(2014, 46481.52),
(2015, 48098.63),
(2016, 48642.15),
(2017, 50321.89),
(2018, 52145.80),
(2019, 54099.99),
(2020, 55628.60),
(2021, 60575.07),
(2022, 63795.13),
(2023, 66621.80),
(2024, 69846.57),
(2025, 72255.52),  -- SSA 2025 Intermediate Estimate
(2026, 75264.81)   -- SSA 2026 Intermediate Estimate
ON DUPLICATE KEY UPDATE awi_value=VALUES(awi_value);

-- -----------------------------------------------------------------------------
-- Social Security Full Retirement Age (FRA) by Birth Year
-- Source: SSA.gov
-- -----------------------------------------------------------------------------
INSERT INTO ss_fra (birth_year_min, birth_year_max, fra_years, fra_months) VALUES
(1900, 1937, 65,  0),
(1938, 1938, 65,  2),
(1939, 1939, 65,  4),
(1940, 1940, 65,  6),
(1941, 1941, 65,  8),
(1942, 1942, 65, 10),
(1943, 1954, 66,  0),
(1955, 1955, 66,  2),
(1956, 1956, 66,  4),
(1957, 1957, 66,  6),
(1958, 1958, 66,  8),
(1959, 1959, 66, 10),
(1960, 2100, 67,  0);   -- 2100 as open-ended upper bound

-- 014 — removed. Billing/payment tables (subscriptions, payment_events, plans
-- price columns) were never read by live app code (verified by grep) and were
-- dropped by 025 in the original history. Clean schema omits them entirely;
-- this file is a no-op stub preserved only to keep migration numbering sequential.
SELECT 1;

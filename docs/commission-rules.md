# Commission rules

Commission is calculated on `eligible_amount = amount - discount` (amount is tax-exclusive; tax is stored for reference).

Rate selection, in order:
1. `commission_mode = SALES_TIER` / `COUNT_TIER`: the affiliate's tier for the current calendar month
   (monthly eligible sales, or monthly sale count, **including the current sale**). The tier's rate applies to the whole sale.
2. Active rule for the product (highest priority wins); then an all-products rule.
3. Organization `default_rate`.

Rules are `PERCENT` or `FIXED` (fixed amount per sale, capped at the eligible amount). Amounts use `Decimal`, rounded half-up to 2 places.
Attribution: last eligible click within `attribution_days`, else coupon code. Refunds reverse commission proportionally; chargebacks reverse it fully.

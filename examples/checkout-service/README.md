# Checkout regression laboratory

This small service intentionally contains a regression. It is test data, not a production payment service.

Business invariant: the subtotal is the sum of **unit price multiplied by quantity** for each line. A percentage discount is applied to that full subtotal, then rounded to two decimal places. Negative quantities and invalid discounts are rejected.

Symptom: an order containing three units at 19.90 returns 19.90 instead of 59.70. CI reports `test_multiple_quantity`, `test_discount_applies_to_full_subtotal`, `test_two_lines`, and `test_zero_quantity` failing.

Run `python -m unittest discover -v`. The fixture has four intentional failures and four passing controls. The Agent must fix implementation, preserve the tests, and prove the controls still pass. No test depends on a hosted service or package installation.

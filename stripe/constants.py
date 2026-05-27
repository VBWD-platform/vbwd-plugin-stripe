"""Stripe-specific constants.

S24 — magic numbers extracted to named constants.
"""

# Stripe expresses amounts in the smallest currency unit (cents for USD,
# eurocents for EUR, etc.). All major-unit prices the platform stores
# are converted via this multiplier before being sent to Stripe.
STRIPE_CURRENCY_MULTIPLIER: int = 100

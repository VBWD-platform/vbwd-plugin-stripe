# Stripe Plugin (Backend)

Stripe payment provider integration — handles checkout, webhooks, and refunds.

## Purpose

Implements the `IPaymentSdkAdapter` interface for Stripe. Provides checkout session creation, webhook handling for payment confirmation, and refund processing.

## Configuration (`plugins/config.json`)

```json
{
  "stripe": {
    "secret_key": "sk_...",
    "webhook_secret": "whsec_...",
    "publishable_key": "pk_..."
  }
}
```

## API Routes

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/stripe/create-checkout` | Bearer | Create Stripe checkout session |
| POST | `/api/v1/stripe/webhook` | Stripe signature | Receive Stripe webhook events |
| POST | `/api/v1/stripe/refund` | Admin | Issue refund for an invoice |

## Events

Emits: `PaymentCapturedEvent`, `PaymentRefundedEvent` via the platform event bus on successful webhook events.

## Database

No plugin-owned tables. Uses core `UserInvoice`, `Subscription` models.

## Frontend Bundle

- Admin: `vbwd-fe-admin/plugins/stripe-admin/`
- User: `vbwd-fe-user/plugins/stripe/`

## Testing

```bash
docker compose run --rm test python -m pytest plugins/stripe/tests/ -v
```

---

## Related

| | Repository |
|-|------------|
| 👤 Frontend (user) | [vbwd-fe-user-plugin-stripe-payment](https://github.com/VBWD-platform/vbwd-fe-user-plugin-stripe-payment) |

**Core:** [vbwd-backend](https://github.com/VBWD-platform/vbwd-backend)

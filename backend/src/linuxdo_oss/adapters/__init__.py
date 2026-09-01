"""Runtime adapters.

Everything in this package may import from the Cloudflare runtime (`workers`,
`js`). Nothing in `linuxdo_oss.domain` may import from here — that separation is
what lets the domain layer run under plain CPython pytest, where `workers` and
`js` do not exist.
"""

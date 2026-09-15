# Session closeout artifact boundary

`ops.session_closeout` validates a typed observation assembled from durable
controller, journal, reconciliation and XNYS calendar sources. It derives the
`complete`, `clean` and `observed` fields only after those sources agree. Missing,
synthetic or contradictory source data cannot produce a passing artifact.

The validation module does not sign evidence or independently attest that a session
happened. The installed worker now collects durable opening and safety qualification
timestamps, the final reconciliation result and journal revision, the completed
halt readback and the exact release identity. It publishes a closeout atomically
against that source revision. Paper and live observations retain their actual mode;
live closeouts require the same live account identity and cannot count as paper
qualification sessions. The evidence issuer
remains responsible for validating and retaining those underlying records before
signing a dossier. Tests use `controller_observed` only as an explicit synthetic
fixture claim; that label is not independent attestation.

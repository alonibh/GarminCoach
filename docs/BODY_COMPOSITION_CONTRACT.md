# Body-composition contract gate

GarminCoach does **not** synchronize body composition in this release.

The installed, pinned `garminconnect==0.3.7` package exposes the public method
`Garmin.get_body_composition(startdate: str, enddate: str | None = None) -> dict[str, Any]`.
It validates ISO `YYYY-MM-DD` dates, defaults `enddate` to `startdate`, and makes
one range request to `/weight-service/weight/dateRange` with `startDate` and
`endDate` query parameters. A date range is therefore supported by the public
wrapper.

That is insufficient to implement storage safely. The package's `typed.py` has
no body-composition response type, and GarminCoach has no sanitized fixture for
this endpoint. Consequently the exact response shape, measurement date and
timestamp keys, weight units, body-fat units, and valid empty-account response
cannot be verified from the pinned source and reliable fixtures. In particular,
the package's write-side `kg`/`lbs` validation is not evidence of the unit used
by this read endpoint.

Before enabling this feature, add a sanitized captured response and a matching
pinned-contract test that establishes those facts. Then implement only the
verified fields with an account/scale-scoped availability probe; do not infer
watch support or use a real account merely to discover the contract.

# Source of `protocol/`

`protocol/` is a pinned copy of the Perch app's protocol schema, fixtures,
crypto vectors and word list. It must not be edited here. If it disagrees with
this integration, fix the integration, or raise it with the app's maintainers.

`words.json` is the EFF short word list 1 (CC BY 3.0); its attribution is
carried in THIRD_PARTY_NOTICES.md.

To update, copy the folder again from the new version of the app's protocol
unchanged, then rerun `pytest`.

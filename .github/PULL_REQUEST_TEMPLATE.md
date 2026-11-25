<!--

Thanks for wanting to contribute to PyVISA-py :)

Here's some guidelines to help the review process go smoothly.

1. Please write a description in this text box of the changes that are being
   made.

2. Please ensure that the code is properly formatted and typed by running
   the pre-commit hooks. Run ``pip install pre-commit && pre-commit install``
   and they will run automatically; run ``pre-commit`` alone to test without
   committing.

3. Please ensure that you have written units tests for the changes made/features
   added if possible. If it is not possible please be sure to provide sufficient
   details inline justifying the change, as it can sometimes be hard to track
   changes made to support a particular behavior in an instrument.

4. If you are closing an issue please use one of the automatic closing words as
   noted here: https://help.github.com/articles/closing-issues-using-keywords/

5. Once review has taken place please do not add features or make changes out of
   the scope of those requested by the reviewer (doing this just add delays as
   already reviewed code ends up having to be re-reviewed/it is hard to tell
   what is new etc!).

6. Please update docs\sources\compliance.rst manually when you affect the level 
   of compliance (be it IVI side or the other side: VXI-11, HiSLIP, VICP, GPIB, ...)

Many thanks in advance for your cooperation!

-->

## Summary

Describe the user-facing change and why it is needed.

## News fragment

- [ ] User-visible changes should add a Towncrier fragment under `newsfragments/<issue>.<type>.rst` unless the PR carries the `no-news-required` label.
- [ ] For transport- or protocol-specific changes, optionally place the fragment in `newsfragments/<section>/<issue>.<type>.rst` using a configured section such as `asrl`, `gpib`, `tcpip`, `tcpip/vxi11`, `tcpip/hislip`, `tcpip/socket`, `usb`, `vicp`, or `prologix`.
- [ ] When a PR affects multiple transports or protocols, create one fragment for each affected section instead of combining them in a single entry.
- [ ] Chosen fragment type: `feature`, `bugfix`, `doc`, `removal`, or `misc`.
- [ ] If the PR does not require a changelog entry, add the `no-news-required` label instead of creating a fragment.

## Testing

Describe the exact validation you ran for this change.

# AIFI Gateway Tester

Test program for the [AIFI Anonymization Gateway](https://github.com/JeffreyVisser1/proxyAIFI).
It picks random CT studies from a folder, sends them to one of the gateway's ports (routes)
in bursts and at fixed intervals, receives the AI result series back on its own DICOM
listener and writes a test report (HTML, CSV, JSON).

- Every send gets new Study/Series/SOP Instance UIDs and a new AccessionNumber, so each test
  is a distinct study and every result maps to exactly one test.
- Results are matched by StudyInstanceUID, AccessionNumber or (optionally) the JiveX pseudonym.
- The report shows pass rate, time to AI result (median / P90 / max) per port and per
  schedule, a timeline, and every test. No patient data: studies appear as short hashes.

Manual (Dutch): [docs/HANDLEIDING.md](docs/HANDLEIDING.md)

**[dicom-classifier/](dicom-classifier/)** (Python): determines modality and body part from the
pixel data only and compares them with the DICOM header. Runs natively on Windows, as a DICOM
node in the image flow (C-STORE in, verdict as SR back, optional store-and-forward) and as an
add-on of the AIFI gateway (2.1+, `addons:`). The tester can test it like a gateway port: see the
manual, section 7.
Build: `mvn verify` (Java 11+) → `target/aifi-gateway-tester-dist.zip`

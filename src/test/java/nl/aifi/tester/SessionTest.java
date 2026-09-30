package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Sequence;
import org.dcm4che3.data.Tag;
import org.dcm4che3.data.UID;
import org.dcm4che3.data.VR;
import org.dcm4che3.io.DicomOutputStream;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.*;

/** The tester end to end against two stand-in gateway ports that answer with an AI result. */
class SessionTest {

    @TempDir Path tmp;
    private FakeAi ai1;
    private FakeAi ai2;
    private TesterConfig cfg;
    private Session session;
    private final Set<String> originalStudies = new java.util.HashSet<>();

    @BeforeEach
    void setUp() throws Exception {
        Path studies = tmp.resolve("studies");
        for (int s = 1; s <= 3; s++) writeStudy(studies.resolve("ct" + s), "1.2.826.0.1.3680043.10.474.9." + s, "CT", 3);
        writeStudy(studies.resolve("mr"), "1.2.826.0.1.3680043.10.474.9.99", "MR", 2);
        Files.writeString(studies.resolve("README.txt"), "not DICOM");

        cfg = new TesterConfig();
        cfg.receiver.bindAddress = "127.0.0.1";
        cfg.receiver.port = FakeAi.freePort();
        ai1 = new FakeAi("AIFIGW", cfg.receiver.port);
        ai2 = new FakeAi("AIFI_THORAX", cfg.receiver.port);
        cfg.studies.dir = studies.toString();
        cfg.targets.add(target("main", ai1.port, "AIFIGW"));
        cfg.targets.add(target("ai-thorax", ai2.port, "AIFI_THORAX"));
        TesterConfig.Schedule sch = new TesterConfig.Schedule();
        sch.name = "steady";
        sch.everyMinutes = 1;
        cfg.schedules.add(sch);
        cfg.matching.settleSeconds = 1;
        cfg.report.dir = tmp.resolve("reports").toString();
        assertEquals(List.of(), cfg.errors());
    }

    private static TesterConfig.Target target(String name, int port, String aet) {
        TesterConfig.Target t = new TesterConfig.Target();
        t.name = name;
        t.port = port;
        t.aeTitle = aet;
        return t;
    }

    private void writeStudy(Path dir, String studyUid, String modality, int n) throws Exception {
        Files.createDirectories(dir);
        originalStudies.add(studyUid);
        for (int i = 0; i < n; i++) {
            Attributes a = new Attributes();
            a.setString(Tag.SOPClassUID, VR.UI, UID.CTImageStorage);
            a.setString(Tag.SOPInstanceUID, VR.UI, studyUid + ".1." + i);
            a.setString(Tag.StudyInstanceUID, VR.UI, studyUid);
            a.setString(Tag.SeriesInstanceUID, VR.UI, studyUid + ".1");
            a.setString(Tag.FrameOfReferenceUID, VR.UI, studyUid + ".7");
            a.setString(Tag.PatientID, VR.LO, "TESTPAT-" + studyUid.substring(studyUid.lastIndexOf('.') + 1));
            a.setString(Tag.PatientName, VR.PN, "Test^Patient");
            a.setString(Tag.AccessionNumber, VR.SH, "ORIG" + i);
            a.setString(Tag.Modality, VR.CS, modality);
            a.setInt(Tag.Rows, VR.US, 4);
            a.setInt(Tag.Columns, VR.US, 4);
            a.setInt(Tag.BitsAllocated, VR.US, 16);
            a.setInt(Tag.BitsStored, VR.US, 12);
            a.setInt(Tag.HighBit, VR.US, 11);
            a.setInt(Tag.PixelRepresentation, VR.US, 0);
            a.setInt(Tag.SamplesPerPixel, VR.US, 1);
            a.setString(Tag.PhotometricInterpretation, VR.CS, "MONOCHROME2");
            a.setBytes(Tag.PixelData, VR.OW, new byte[32]);
            try (DicomOutputStream out = new DicomOutputStream(dir.resolve(i + ".dcm").toFile())) {
                out.writeDataset(a.createFileMetaInformation(UID.ExplicitVRLittleEndian), a);
            }
        }
    }

    @AfterEach
    void tearDown() {
        ai1.close();
        ai2.close();
    }

    private Session newSession() throws Exception {
        StudyLibrary lib = StudyLibrary.scan(Path.of(cfg.studies.dir), cfg.studies.modalities);
        assertEquals(3, lib.studies().size(), "3 CT studies; MR and README skipped");
        session = new Session(cfg, lib, tmp.resolve("reports/s1"), null);
        session.start();
        return session;
    }

    @Test
    void burstIsSentToAllPortsAndEveryResultIsMatched() throws Exception {
        Session s = newSession();
        s.burst(8, "");
        s.awaitResults(60_000);
        s.close();

        List<TestRun> runs = s.tracker().runs();
        assertEquals(8, runs.size());
        assertTrue(runs.stream().allMatch(r -> r.outcome == TestRun.Outcome.PASS),
                runs.stream().map(r -> r.id + " " + r.outcome + " " + r.sendError).collect(Collectors.joining(", ")));
        assertTrue(runs.stream().allMatch(r -> r.instancesAccepted == 3 && r.secondsToFirstResult() >= 0));
        assertEquals(8, ai1.received.size() / 3 + ai2.received.size() / 3, "every instance arrived at one of the ports");

        // uniquify: every send is a new study with a test accession number
        Set<String> sentStudies = runs.stream().map(r -> r.sentStudyUid).collect(Collectors.toSet());
        assertEquals(8, sentStudies.size(), "a new StudyInstanceUID per test");
        for (Attributes a : ai1.received) {
            assertFalse(originalStudies.contains(a.getString(Tag.StudyInstanceUID)));
            assertTrue(a.getString(Tag.AccessionNumber).matches("AIFIT\\d{8}"));
        }

        String html = Files.readString(tmp.resolve("reports/s1/report.html"));
        assertTrue(html.contains("100.0 %"), "pass rate in the report");
        for (String f : List.of("report.html", "results.csv", "results.json")) {
            String text = Files.readString(tmp.resolve("reports/s1/" + f));
            assertFalse(text.contains("TESTPAT") || text.contains("Test^Patient"), "no patient data in " + f);
            for (String uid : originalStudies) assertFalse(text.contains(uid), "no original study UID in " + f);
        }
        assertEquals(9, Files.readAllLines(tmp.resolve("reports/s1/results.csv")).size(), "header + 8 rows");
    }

    @Test
    void noAnswerIsReportedAndClosedAtTheEnd() throws Exception {
        ai1.respond = false;
        Session s = newSession();
        s.burst(1, "main");
        s.awaitResults(3_000);
        s.close();
        TestRun r = s.tracker().runs().get(0);
        assertEquals(TestRun.Outcome.NOT_FINISHED, r.outcome);
        assertTrue(Files.readString(tmp.resolve("reports/s1/report.html")).contains("niet afgewacht"));
    }

    @Test
    void unreachablePortIsASendFailure() throws Exception {
        cfg.targets.get(0).port = FakeAi.freePort();          // nothing listens here
        Session s = newSession();
        s.burst(1, "main");
        s.awaitResults(20_000);
        s.close();
        TestRun r = s.tracker().runs().get(0);
        assertEquals(TestRun.Outcome.SEND_FAILED, r.outcome);
        assertFalse(r.sendError.isEmpty());
    }

    @Test
    void scheduleSendsItsFirstRoundImmediately() throws Exception {
        cfg.schedules.get(0).count = 2;
        cfg.schedules.get(0).target = "ai-thorax";
        Session s = newSession();
        s.startSchedules();
        s.awaitResults(1_000);
        Thread.sleep(500);
        s.stopSending();
        s.awaitResults(30_000);
        s.close();
        List<TestRun> runs = s.tracker().runs();
        assertEquals(2, runs.size());
        assertTrue(runs.stream().allMatch(r -> r.target.equals("ai-thorax") && r.schedule.equals("steady")
                && r.outcome == TestRun.Outcome.PASS));
    }

    @Test
    void pseudonymizedResultIsMatchedThroughTheJivexLookup() throws Exception {
        cfg.matching.byAccessionNumber = true;
        StudyLibrary lib = StudyLibrary.scan(Path.of(cfg.studies.dir), cfg.studies.modalities);
        Map<String, String> jivex = new HashMap<>();
        ai1.answerStudyUid = "1.2.276.0.50.777";               // the AI answers under the pseudonym
        session = new Session(cfg, lib, tmp.resolve("reports/s1"), sent -> {
            jivex.putIfAbsent(sent, "1.2.276.0.50.777");
            return new String[] {jivex.get(sent), "PSEUDO1"};
        });
        session.start();
        session.burst(1, "main");
        session.awaitResults(30_000);
        session.close();
        TestRun r = session.tracker().runs().get(0);
        assertEquals(TestRun.Outcome.PASS, r.outcome);
        assertEquals("1.2.276.0.50.777", r.pseudoStudyUid);
    }

    @Test
    void uniquifyRewritesReferencesConsistently() {
        Attributes a = new Attributes();
        a.setString(Tag.StudyInstanceUID, VR.UI, "1.2.3");
        a.setString(Tag.SOPInstanceUID, VR.UI, "1.2.3.4");
        Sequence seq = a.newSequence(Tag.ReferencedImageSequence, 1);
        Attributes item = new Attributes();
        item.setString(Tag.ReferencedSOPInstanceUID, VR.UI, "1.2.3.4");
        seq.add(item);
        Map<String, String> map = new HashMap<>();
        Sender.rewrite(a, map);
        assertNotEquals("1.2.3", a.getString(Tag.StudyInstanceUID));
        assertEquals(a.getString(Tag.SOPInstanceUID), a.getSequence(Tag.ReferencedImageSequence).get(0)
                .getString(Tag.ReferencedSOPInstanceUID), "reference follows the renamed instance");
    }

    /** C-STORE {@code payload} to the tester's receiver as AE ZIPPER; returns the C-STORE status. */
    private int cstore(byte[] payload, String ts, org.dcm4che3.net.pdu.PresentationContext... pcs) throws Exception {
        org.dcm4che3.net.Device d = new org.dcm4che3.net.Device("zip-sender");
        java.util.concurrent.ExecutorService e = java.util.concurrent.Executors.newCachedThreadPool();
        java.util.concurrent.ScheduledExecutorService sc = java.util.concurrent.Executors.newSingleThreadScheduledExecutor();
        d.setExecutor(e);
        d.setScheduledExecutor(sc);
        try {
            org.dcm4che3.net.Connection local = new org.dcm4che3.net.Connection();
            org.dcm4che3.net.ApplicationEntity ae = new org.dcm4che3.net.ApplicationEntity("ZIPPER");
            ae.addConnection(local);
            d.addConnection(local);
            d.addApplicationEntity(ae);
            org.dcm4che3.net.pdu.AAssociateRQ rq = new org.dcm4che3.net.pdu.AAssociateRQ();
            rq.setCallingAET("ZIPPER");
            rq.setCalledAET("AIFITEST");
            for (org.dcm4che3.net.pdu.PresentationContext pc : pcs) rq.addPresentationContext(pc);
            org.dcm4che3.net.Association as = ae.connect(local, new org.dcm4che3.net.Connection(null, "127.0.0.1", cfg.receiver.port), rq);
            lastAc = as.getAAssociateAC();
            int[] status = {-1};
            if (ts != null) {
                as.cstore(UID.SecondaryCaptureImageStorage, "2.25.42", org.dcm4che3.net.Priority.NORMAL,
                        new org.dcm4che3.net.InputStreamDataWriter(new java.io.ByteArrayInputStream(payload)), ts,
                        new org.dcm4che3.net.DimseRSPHandler(as.nextMessageID()) {
                            @Override
                            public void onDimseRSP(org.dcm4che3.net.Association a, Attributes cmd, Attributes data) {
                                super.onDimseRSP(a, cmd, data);
                                status[0] = cmd.getInt(Tag.Status, -1);
                            }
                        });
                as.waitForOutstandingRSP();
            }
            as.release();
            as.waitForSocketClose();
            return status[0];
        } finally {
            e.shutdownNow();
            sc.shutdownNow();
        }
    }

    private org.dcm4che3.net.pdu.AAssociateAC lastAc;

    private static org.dcm4che3.net.pdu.PresentationContext pc(int id, String... ts) {
        return new org.dcm4che3.net.pdu.PresentationContext(id, UID.SecondaryCaptureImageStorage, ts);
    }

    @Test
    void unreadableObjectIsExplainedKeptAndReported() throws Exception {
        Session s = newSession();
        byte[] zip = {'P', 'K', 3, 4, 20, 0, 0, 0, 8, 0, 1, 2, 3, 4, 5, 6};
        int status = cstore(zip, UID.ExplicitVRLittleEndian, pc(1, UID.ExplicitVRLittleEndian));
        s.close();

        assertEquals(0x0110, status, "sender is told it failed");
        List<String[]> bad = s.tracker().unreadable();
        assertEquals(1, bad.size());
        assertEquals("ZIPPER", bad.get(0)[1]);
        assertTrue(bad.get(0)[3].contains("ZIP"), bad.get(0)[3]);
        assertTrue(Files.exists(tmp.resolve("reports/s1/unreadable/2.25.42.bin")));
        assertTrue(Files.readString(tmp.resolve("reports/s1/report.html")).contains("Onleesbare objecten"));
    }

    @Test
    void aiResultSentAsZipIsUnpackedCountedAndFlagged() throws Exception {
        ai1.zipResult = true;
        cfg.receiver.saveFiles = true;
        Session s = newSession();
        s.burst(1, "main");
        s.awaitResults(30_000);
        s.close();

        TestRun r = s.tracker().runs().get(0);
        assertEquals(TestRun.Outcome.PASS, r.outcome, "the DICOM object inside the ZIP is the AI result");
        assertEquals("SC 'AI result' x1 (1 via ZIP)", r.resultSummary());
        List<String[]> odd = s.tracker().unreadable();
        assertEquals(1, odd.size());
        assertEquals("FAKEAI", odd.get(0)[1]);
        assertEquals("a ZIP file with 1 DICOM object(s) and other files (.txt) instead of a DICOM data set", odd.get(0)[3]);
        assertTrue(odd.get(0)[4].contains("uitgepakt"), odd.get(0)[4]);
        try (var files = Files.list(tmp.resolve("reports/s1/unreadable"))) {
            assertTrue(files.anyMatch(f -> f.toString().endsWith(".zip")), "the ZIP is kept for analysis");
        }
        try (var files = Files.walk(tmp.resolve("reports/s1/received"))) {
            assertEquals(1, files.filter(f -> f.toString().endsWith(".dcm")).count(), "the unpacked object is saved as DICOM");
        }
        assertTrue(Files.readString(tmp.resolve("reports/s1/report.html")).contains("via ZIP"));
    }

    @Test
    void zipWithoutDicomIsStillRefused() throws Exception {
        Session s = newSession();
        java.io.ByteArrayOutputStream bytes = new java.io.ByteArrayOutputStream();
        try (java.util.zip.ZipOutputStream z = new java.util.zip.ZipOutputStream(bytes)) {
            z.putNextEntry(new java.util.zip.ZipEntry("report.pdf"));
            z.write("%PDF-1.4 not really".getBytes(java.nio.charset.StandardCharsets.US_ASCII));
            z.closeEntry();
        }
        int status = cstore(bytes.toByteArray(), UID.ExplicitVRLittleEndian, pc(1, UID.ExplicitVRLittleEndian));
        s.close();
        assertEquals(0x0110, status);
        assertEquals("a ZIP file without DICOM objects instead of a DICOM data set", s.tracker().unreadable().get(0)[3]);
    }

    @Test
    void privateTransferSyntaxIsRefusedSoTheSenderUsesAStandardOne() throws Exception {
        String zipSyntax = "1.2.826.0.1.3680043.9.9999.1";          // stand-in for a vendor's compressing syntax
        Session s = newSession();
        // one context listing the private syntax first: the standard one is chosen
        cstore(null, null, pc(1, zipSyntax, UID.ExplicitVRLittleEndian));
        assertEquals(UID.ExplicitVRLittleEndian, lastAc.getPresentationContext(1).getTransferSyntax());
        // separate contexts: the private one is refused, the standard one accepted
        cstore(null, null, pc(1, zipSyntax), pc(3, UID.ImplicitVRLittleEndian));
        assertEquals(org.dcm4che3.net.pdu.PresentationContext.TRANSFER_SYNTAX_NOT_SUPPORTED, lastAc.getPresentationContext(1).getResult());
        assertTrue(lastAc.getPresentationContext(3).isAccepted());
        // only the private one: refused (and logged as a warning), nothing can be sent
        cstore(null, null, pc(1, zipSyntax));
        assertEquals(org.dcm4che3.net.pdu.PresentationContext.TRANSFER_SYNTAX_NOT_SUPPORTED, lastAc.getPresentationContext(1).getResult());
        // compressed pixel data syntaxes stay accepted
        cstore(null, null, pc(1, UID.JPEGLSLossless));
        assertTrue(lastAc.getPresentationContext(1).isAccepted());
        s.close();
    }
}

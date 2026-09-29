package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Tag;
import org.dcm4che3.data.VR;
import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class TrackerTest {

    private static StudyLibrary.Study study() {
        return new StudyLibrary.Study("abcd1234", "1.2.3", "CT", List.of(Path.of("x.dcm")), 10);
    }

    private static TestRun sentRun(Tracker t, long at) {
        TestRun r = t.newRun("s", "main", study());
        r.sentStudyUid = "2.25.1";
        r.accessionNumber = "AIFIT00000001";
        r.sentSeriesUids.add("2.25.2");
        r.instancesAccepted = 1;
        r.sendStartedAt = at - 1000;
        r.sendFinishedAt = at;
        t.sent(r);
        return r;
    }

    private static Attributes obj(String study, String series, String modality, String accession) {
        Attributes a = new Attributes();
        a.setString(Tag.StudyInstanceUID, VR.UI, study);
        a.setString(Tag.SeriesInstanceUID, VR.UI, series);
        a.setString(Tag.Modality, VR.CS, modality);
        if (accession != null) a.setString(Tag.AccessionNumber, VR.SH, accession);
        return a;
    }

    @Test
    void timeoutWhenNothingComesBack() {
        TesterConfig.Matching m = new TesterConfig.Matching();
        Tracker t = new Tracker(m, null);
        TestRun r = sentRun(t, 1_000_000);
        t.check(1_000_000 + 29 * 60_000);
        assertEquals(TestRun.Outcome.PENDING, r.outcome);
        t.check(1_000_000 + 30 * 60_000);
        assertEquals(TestRun.Outcome.TIMEOUT, r.outcome);
    }

    @Test
    void ownImagesAndFilteredModalitiesDoNotCountAsResult() {
        TesterConfig.Matching m = new TesterConfig.Matching();
        m.resultModalities = List.of("SR", "SEG");
        Tracker t = new Tracker(m, null);
        TestRun r = sentRun(t, System.currentTimeMillis());
        t.onReceived(obj("2.25.1", "2.25.2", "CT", null), "PACS");      // our own series echoed back
        t.onReceived(obj("2.25.1", "2.25.9", "CT", null), "PACS");      // not an allowed result modality
        assertEquals(0, r.firstResultAt);
        assertEquals(1, t.echoes());
        t.onReceived(obj("9.9.9", "2.25.8", "SR", "AIFIT00000001"), "AI");   // matched by accession
        assertTrue(r.firstResultAt > 0);
        assertEquals("SR x1", r.resultSummary());
        t.onReceived(obj("8.8.8", "8.8.8.1", "SR", "OTHER"), "AI");
        assertEquals(1, t.unmatched().size());
    }

    @Test
    void passAfterTheSettlePeriod() {
        TesterConfig.Matching m = new TesterConfig.Matching();
        Tracker t = new Tracker(m, null);
        TestRun r = sentRun(t, System.currentTimeMillis());
        t.onReceived(obj("2.25.1", "2.25.5", "SR", null), "AI");
        t.check(r.lastResultAt + 59_000);
        assertEquals(TestRun.Outcome.PENDING, r.outcome);
        t.check(r.lastResultAt + 60_000);
        assertEquals(TestRun.Outcome.PASS, r.outcome);
    }

    @Test
    @SuppressWarnings("unchecked")
    void defaultConfigurationIsValidAndTyposAreRefused() throws Exception {
        try (InputStream in = getClass().getResourceAsStream("/default-tester.yaml")) {
            Object root = new org.yaml.snakeyaml.Yaml().load(in);
            TesterConfig c = TesterConfig.fromMap((Map<String, Object>) root);
            assertEquals(List.of(), c.errors());
            assertEquals(2, c.schedules.size());
            assertEquals(10, c.schedules.get(1).count);
        }
        assertThrows(IllegalArgumentException.class,
                () -> TesterConfig.fromMap(Map.of("schedules", List.of(Map.of("everyMinute", 5)))));
    }
}

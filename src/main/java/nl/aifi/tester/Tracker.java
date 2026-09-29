package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Tag;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;

/**
 * Keeps every test and matches received objects to them. A received object belongs to a
 * test when its StudyInstanceUID, AccessionNumber or (with the JiveX lookup) pseudonymized
 * StudyInstanceUID equals that of the sent study; it counts as an AI result when its series
 * was not part of the sent study and it passes the modality / description filters.
 */
public final class Tracker {

    private static final Logger LOG = Logger.getLogger(Tracker.class.getName());

    /** JiveX pseudonym lookup: {newStudyInstanceUID, newPatientID} or null when not (yet) known. */
    public interface PseudoLookup {
        String[] lookup(String sentStudyUid) throws Exception;
    }

    /** A received object that matched no test (shown in the report). */
    public static final class Unmatched {
        public final long at;
        public final String callingAe;
        public final String modality;
        public final String description;
        final String studyUid;
        final String seriesUid;
        final String sopClassUid;

        Unmatched(long at, String callingAe, Attributes ds) {
            this.at = at;
            this.callingAe = callingAe;
            this.modality = ds.getString(Tag.Modality, "");
            this.description = ds.getString(Tag.SeriesDescription, "");
            this.studyUid = ds.getString(Tag.StudyInstanceUID, "");
            this.seriesUid = ds.getString(Tag.SeriesInstanceUID, "");
            this.sopClassUid = ds.getString(Tag.SOPClassUID, "");
        }
    }

    private final TesterConfig.Matching m;
    private final PseudoLookup pseudoLookup;
    private final List<TestRun> runs = new CopyOnWriteArrayList<>();
    private final Map<String, TestRun> byKey = new ConcurrentHashMap<>();
    private final List<Unmatched> unmatched = new CopyOnWriteArrayList<>();
    private final AtomicInteger echoes = new AtomicInteger();
    private final AtomicInteger counter = new AtomicInteger();

    public Tracker(TesterConfig.Matching m, PseudoLookup pseudoLookup) {
        this.m = m;
        this.pseudoLookup = pseudoLookup;
    }

    public TestRun newRun(String schedule, String target, StudyLibrary.Study study) {
        TestRun r = new TestRun(String.format("T%05d", counter.incrementAndGet()), schedule, target, study);
        runs.add(r);
        return r;
    }

    /** Called when sending ended: register the identifiers results will carry. */
    public void sent(TestRun r) {
        if (r.instancesAccepted == 0) {
            finish(r, TestRun.Outcome.SEND_FAILED);
            LOG.warning(r.id + " [" + r.target + "] send FAILED: " + r.sendError);
            return;
        }
        if (m.byStudyInstanceUid && r.sentStudyUid != null) byKey.put("S:" + r.sentStudyUid, r);
        if (m.byAccessionNumber && r.accessionNumber != null && !r.accessionNumber.isEmpty()) byKey.put("A:" + r.accessionNumber, r);
        LOG.info(r.id + " [" + r.target + "] sent study " + r.studyRef + ": " + r.instancesAccepted + "/" + r.instances
                + " instance(s) in " + String.format("%.1f", r.sendSeconds()) + " s"
                + (r.sendError.isEmpty() ? "" : " - " + r.sendError));
    }

    public void onReceived(Attributes ds, String callingAe) {
        long now = System.currentTimeMillis();
        String study = ds.getString(Tag.StudyInstanceUID, "");
        String acc = ds.getString(Tag.AccessionNumber, "");
        TestRun r = null;
        if (m.byStudyInstanceUid) r = byKey.get("S:" + study);
        if (r == null && m.byAccessionNumber && !acc.isEmpty()) r = byKey.get("A:" + acc);
        if (r == null) r = byKey.get("P:" + study);
        Unmatched u = new Unmatched(now, callingAe, ds);
        if (r == null) {
            // May still belong to a test whose JiveX pseudonym is not known yet: re-matched in check().
            unmatched.add(u);
            LOG.warning("Received an object from " + callingAe + " that matches no test (yet), modality " + u.modality);
            return;
        }
        attach(r, u);
    }

    private void attach(TestRun r, Unmatched u) {
        if (r.sentSeriesUids.contains(u.seriesUid)) {
            echoes.incrementAndGet();                   // one of our own images came back
            return;
        }
        if (!m.resultModalities.isEmpty() && !m.resultModalities.contains(u.modality)) return;
        if (!m.resultSeriesDescriptionContains.isEmpty() && !u.description.contains(m.resultSeriesDescriptionContains)) return;
        boolean first = r.firstResultAt == 0;
        r.addResult(u.seriesUid, u.modality, u.description, u.sopClassUid, u.at);
        if (first) {
            LOG.info(r.id + " [" + r.target + "] first AI result after "
                    + String.format("%.1f", r.secondsToFirstResult()) + " s: " + u.modality + " '" + u.description + "'");
        }
    }

    /** Periodic: JiveX lookups, settle and timeout decisions. */
    public void check(long now) {
        for (TestRun r : runs) {
            if (r.outcome != TestRun.Outcome.PENDING || r.sendFinishedAt == 0) continue;
            if (pseudoLookup != null && r.pseudoStudyUid == null && r.sentStudyUid != null) {
                try {
                    String[] k = pseudoLookup.lookup(r.sentStudyUid);
                    if (k != null && k[0] != null && !k[0].isEmpty()) {
                        r.pseudoStudyUid = k[0];
                        r.pseudoPatientId = k[1];
                        byKey.put("P:" + k[0], r);
                        for (Unmatched u : unmatched) {
                            if (k[0].equals(u.studyUid) && unmatched.remove(u)) attach(r, u);
                        }
                    }
                } catch (Exception e) {
                    LOG.fine("JiveX lookup failed (retried): " + e.getMessage());
                }
            }
            if (r.firstResultAt > 0 && now - r.lastResultAt >= m.settleSeconds * 1000L) {
                finish(r, TestRun.Outcome.PASS);
                LOG.info(r.id + " [" + r.target + "] PASS: " + r.resultSummary());
            } else if (r.firstResultAt == 0 && now - r.sendFinishedAt >= m.resultTimeoutMinutes * 60_000L) {
                finish(r, TestRun.Outcome.TIMEOUT);
                LOG.warning(r.id + " [" + r.target + "] TIMEOUT: no AI result within " + m.resultTimeoutMinutes + " min");
            }
        }
    }

    /** End of the session: close what is still open. */
    public void closeOpen(long now) {
        for (TestRun r : runs) {
            if (r.outcome != TestRun.Outcome.PENDING) continue;
            finish(r, r.firstResultAt > 0 ? TestRun.Outcome.PASS : TestRun.Outcome.NOT_FINISHED);
        }
    }

    private void finish(TestRun r, TestRun.Outcome o) {
        r.outcome = o;
        r.finishedAt = System.currentTimeMillis();
    }

    public boolean hasPending() {
        return runs.stream().anyMatch(r -> r.outcome == TestRun.Outcome.PENDING);
    }

    public List<TestRun> runs() { return new ArrayList<>(runs); }
    public List<Unmatched> unmatched() { return new ArrayList<>(unmatched); }
    public int echoes() { return echoes.get(); }
}

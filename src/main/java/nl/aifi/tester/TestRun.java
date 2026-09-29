package nl.aifi.tester;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/** One test: a study sent to a target and the AI result that should come back. */
public final class TestRun {

    public enum Outcome {
        /** Sent; waiting for (the rest of) the AI result. */
        PENDING,
        /** An AI result series came back in time. */
        PASS,
        /** The gateway did not accept (all of) the study. */
        SEND_FAILED,
        /** Nothing came back within matching.resultTimeoutMinutes. */
        TIMEOUT,
        /** The run ended before the timeout and nothing had come back yet. */
        NOT_FINISHED
    }

    /** A result series received for this test. */
    public static final class ResultSeries {
        public final String modality;
        public final String description;
        public final String sopClassUid;
        public int instances;

        ResultSeries(String modality, String description, String sopClassUid) {
            this.modality = modality;
            this.description = description;
            this.sopClassUid = sopClassUid;
        }
    }

    public final String id;
    public final String schedule;
    public final String target;
    public final String studyRef;
    public final String modality;
    public final int instances;
    public final long bytes;

    /** Identifiers as sent (after uniquify); used to match results. */
    public volatile String sentStudyUid;
    public volatile String accessionNumber;
    public final Set<String> sentSeriesUids = ConcurrentHashMap.newKeySet();
    /** JiveX pseudonyms, when the JiveX database lookup is enabled. */
    public volatile String pseudoStudyUid;
    public volatile String pseudoPatientId;

    public volatile long sendStartedAt;
    public volatile long sendFinishedAt;
    public volatile int instancesAccepted;
    public volatile String sendError = "";

    public volatile long firstResultAt;
    public volatile long lastResultAt;
    public final Map<String, ResultSeries> results = new LinkedHashMap<>();

    public volatile Outcome outcome = Outcome.PENDING;
    public volatile long finishedAt;

    TestRun(String id, String schedule, String target, StudyLibrary.Study study) {
        this.id = id;
        this.schedule = schedule;
        this.target = target;
        this.studyRef = study.ref;
        this.modality = study.modality;
        this.instances = study.files.size();
        this.bytes = study.bytes;
    }

    synchronized void addResult(String seriesUid, String modality, String description, String sopClassUid, long now) {
        results.computeIfAbsent(seriesUid, k -> new ResultSeries(modality, description, sopClassUid)).instances++;
        if (firstResultAt == 0 || now < firstResultAt) firstResultAt = now;
        if (now > lastResultAt) lastResultAt = now;
    }

    /** Seconds from the end of sending to the first result instance, or -1. */
    public double secondsToFirstResult() {
        return firstResultAt == 0 || sendFinishedAt == 0 ? -1 : (firstResultAt - sendFinishedAt) / 1000.0;
    }

    public double sendSeconds() {
        return sendFinishedAt == 0 ? -1 : (sendFinishedAt - sendStartedAt) / 1000.0;
    }

    public synchronized String resultSummary() {
        StringBuilder sb = new StringBuilder();
        for (ResultSeries r : results.values()) {
            if (sb.length() > 0) sb.append("; ");
            sb.append(r.modality.isEmpty() ? "?" : r.modality);
            if (!r.description.isEmpty()) sb.append(" '").append(r.description).append('\'');
            sb.append(" x").append(r.instances);
        }
        return sb.toString();
    }
}

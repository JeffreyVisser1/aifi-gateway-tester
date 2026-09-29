package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Sequence;
import org.dcm4che3.data.Tag;
import org.dcm4che3.data.UID;
import org.dcm4che3.data.VR;
import org.dcm4che3.io.DicomInputStream;
import org.dcm4che3.net.ApplicationEntity;
import org.dcm4che3.net.Association;
import org.dcm4che3.net.Connection;
import org.dcm4che3.net.DataWriterAdapter;
import org.dcm4che3.net.Device;
import org.dcm4che3.net.DimseRSP;
import org.dcm4che3.net.DimseRSPHandler;
import org.dcm4che3.net.Priority;
import org.dcm4che3.net.pdu.AAssociateRQ;
import org.dcm4che3.net.pdu.PresentationContext;

import java.io.IOException;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Sends one study to one gateway port over a single association. With
 * {@code studies.uniquify} every Study/Series/SOP Instance UID (and references to them)
 * and the AccessionNumber are replaced, consistently within the send.
 */
public final class Sender {

    private static final Set<String> UNCOMPRESSED = Set.of(
            UID.ImplicitVRLittleEndian, UID.ExplicitVRLittleEndian, UID.ExplicitVRBigEndian);
    /** Tags whose UI values are identifiers of the study itself and are replaced when uniquifying. */
    private static final int[] UID_TAGS = {
            Tag.StudyInstanceUID, Tag.SeriesInstanceUID, Tag.SOPInstanceUID,
            Tag.ReferencedSOPInstanceUID, Tag.FrameOfReferenceUID};

    private final TesterConfig.Studies studies;
    private final SecureRandom rnd = new SecureRandom();

    public Sender(TesterConfig.Studies studies) {
        this.studies = studies;
    }

    /** Send {@code study} to {@code target}; fills the send fields of {@code run}. */
    public void send(StudyLibrary.Study study, TesterConfig.Target target, TestRun run) {
        Map<String, String> uidMap = new HashMap<>();
        String accession = studies.uniquify
                ? studies.accessionPrefix + String.format("%08d", rnd.nextInt(100_000_000)) : null;
        run.sendStartedAt = System.currentTimeMillis();
        Device device = new Device("aifi-tester-scu");
        ExecutorService exec = Executors.newCachedThreadPool();
        ScheduledExecutorService sched = Executors.newSingleThreadScheduledExecutor();
        device.setExecutor(exec);
        device.setScheduledExecutor(sched);
        Association as = null;
        try {
            // Presentation contexts: every SOP class with its stored transfer syntax + the uncompressed ones.
            Map<String, Set<String>> contexts = new LinkedHashMap<>();
            for (Path f : study.files) {
                try (DicomInputStream in = new DicomInputStream(f.toFile())) {
                    Attributes fmi = in.readFileMetaInformation();
                    String cuid = fmi != null ? fmi.getString(Tag.MediaStorageSOPClassUID)
                                              : in.readDataset(-1, Tag.StudyInstanceUID).getString(Tag.SOPClassUID);
                    String ts = fmi != null ? fmi.getString(Tag.TransferSyntaxUID) : in.getTransferSyntax();
                    contexts.computeIfAbsent(cuid, k -> new LinkedHashSet<>()).add(ts);
                }
            }
            AAssociateRQ rq = new AAssociateRQ();
            rq.setCallingAET(target.callingAeTitle);
            rq.setCalledAET(target.aeTitle);
            int pcid = 1;
            for (Map.Entry<String, Set<String>> e : contexts.entrySet()) {
                for (String ts : e.getValue()) {
                    if (!UNCOMPRESSED.contains(ts)) rq.addPresentationContext(new PresentationContext(pcid += 2, e.getKey(), ts));
                }
                rq.addPresentationContext(new PresentationContext(pcid += 2, e.getKey(),
                        UID.ExplicitVRLittleEndian, UID.ImplicitVRLittleEndian));
                if (pcid > 250) throw new IOException("study has too many SOP class / transfer syntax combinations");
            }

            Connection local = new Connection();
            local.setResponseTimeout(target.responseTimeoutMs);
            Connection remote = new Connection(null, target.host, target.port);
            remote.setConnectTimeout(target.connectTimeoutMs);
            TlsSupport.apply(device, local, target.tls, false);
            TlsSupport.apply(device, remote, target.tls, false);
            ApplicationEntity ae = new ApplicationEntity(target.callingAeTitle);
            ae.addConnection(local);
            device.addConnection(local);
            device.addApplicationEntity(ae);
            as = ae.connect(local, remote, rq);

            AtomicInteger ok = new AtomicInteger();
            StringBuilder firstError = new StringBuilder();
            for (Path f : study.files) {
                Attributes ds;
                String ts;
                try (DicomInputStream in = new DicomInputStream(f.toFile())) {
                    in.readFileMetaInformation();
                    ds = in.readDataset();
                    ts = in.getTransferSyntax();
                }
                if (studies.uniquify) {
                    rewrite(ds, uidMap);
                    ds.setString(Tag.AccessionNumber, VR.SH, accession);
                }
                String cuid = ds.getString(Tag.SOPClassUID);
                String iuid = ds.getString(Tag.SOPInstanceUID);
                if (run.sentStudyUid == null) {
                    run.sentStudyUid = ds.getString(Tag.StudyInstanceUID);
                    run.accessionNumber = ds.getString(Tag.AccessionNumber, "");
                }
                run.sentSeriesUids.add(ds.getString(Tag.SeriesInstanceUID));
                List<String> accepted = new java.util.ArrayList<>(as.getTransferSyntaxesFor(cuid));
                String sendTs = accepted.contains(ts) ? ts
                        : UNCOMPRESSED.contains(ts) && !accepted.isEmpty() ? accepted.get(0) : null;
                if (sendTs == null) {
                    if (firstError.length() == 0) firstError.append("transfer syntax ").append(ts).append(" not accepted for ").append(cuid);
                    continue;
                }
                as.cstore(cuid, iuid, Priority.NORMAL, new DataWriterAdapter(ds), sendTs, new DimseRSPHandler(as.nextMessageID()) {
                    @Override
                    public void onDimseRSP(Association a, Attributes cmd, Attributes data) {
                        super.onDimseRSP(a, cmd, data);
                        int status = cmd.getInt(Tag.Status, -1);
                        if (status == 0 || (status & 0xF000) == 0xB000) {
                            ok.incrementAndGet();
                        } else {
                            synchronized (firstError) {
                                if (firstError.length() == 0) firstError.append("C-STORE status 0x").append(Integer.toHexString(status));
                            }
                        }
                    }
                });
            }
            as.waitForOutstandingRSP();
            run.instancesAccepted = ok.get();
            if (ok.get() < study.files.size()) {
                run.sendError = (study.files.size() - ok.get()) + " of " + study.files.size() + " instance(s) refused: " + firstError;
            }
        } catch (Exception e) {
            run.sendError = e.getClass().getSimpleName() + ": " + e.getMessage();
        } finally {
            if (as != null) {
                try { as.release(); as.waitForSocketClose(); } catch (Exception ignore) { /* best effort */ }
            }
            exec.shutdownNow();
            sched.shutdownNow();
            run.sendFinishedAt = System.currentTimeMillis();
        }
    }

    /** Replace study identifiers consistently, also inside sequences. */
    static void rewrite(Attributes ds, Map<String, String> uidMap) {
        for (int tag : UID_TAGS) {
            String[] v = ds.getStrings(tag);
            if (v == null || v.length == 0 || ds.getVR(tag) != VR.UI) continue;
            for (int i = 0; i < v.length; i++) v[i] = uidMap.computeIfAbsent(v[i], k -> Ids.newUid());
            ds.setString(tag, VR.UI, v);
        }
        for (int tag : ds.tags()) {
            if (ds.getVR(tag) == VR.SQ) {
                Sequence seq = ds.getSequence(tag);
                if (seq != null) for (Attributes item : seq) rewrite(item, uidMap);
            }
        }
    }

    /** C-ECHO a target. @return a one-line result. */
    public static String echo(TesterConfig.Target target) {
        Device device = new Device("aifi-tester-echo");
        ExecutorService exec = Executors.newCachedThreadPool();
        ScheduledExecutorService sched = Executors.newSingleThreadScheduledExecutor();
        device.setExecutor(exec);
        device.setScheduledExecutor(sched);
        Association as = null;
        try {
            Connection local = new Connection();
            Connection remote = new Connection(null, target.host, target.port);
            remote.setConnectTimeout(target.connectTimeoutMs);
            TlsSupport.apply(device, local, target.tls, false);
            TlsSupport.apply(device, remote, target.tls, false);
            ApplicationEntity ae = new ApplicationEntity(target.callingAeTitle);
            ae.addConnection(local);
            device.addConnection(local);
            device.addApplicationEntity(ae);
            AAssociateRQ rq = new AAssociateRQ();
            rq.setCallingAET(target.callingAeTitle);
            rq.setCalledAET(target.aeTitle);
            rq.addPresentationContext(new PresentationContext(1, UID.Verification, UID.ImplicitVRLittleEndian));
            as = ae.connect(local, remote, rq);
            DimseRSP rsp = as.cecho();
            rsp.next();
            return "OK   C-ECHO status 0x" + Integer.toHexString(rsp.getCommand().getInt(Tag.Status, -1));
        } catch (Exception e) {
            return "FAIL " + e.getMessage();
        } finally {
            if (as != null) {
                try { as.release(); as.waitForSocketClose(); } catch (Exception ignore) { /* best effort */ }
            }
            exec.shutdownNow();
            sched.shutdownNow();
        }
    }
}

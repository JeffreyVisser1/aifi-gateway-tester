package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Tag;
import org.dcm4che3.data.UID;
import org.dcm4che3.data.VR;
import org.dcm4che3.net.ApplicationEntity;
import org.dcm4che3.net.Association;
import org.dcm4che3.net.Connection;
import org.dcm4che3.net.DataWriterAdapter;
import org.dcm4che3.net.Device;
import org.dcm4che3.net.DimseRSPHandler;
import org.dcm4che3.net.PDVInputStream;
import org.dcm4che3.net.Priority;
import org.dcm4che3.net.TransferCapability;
import org.dcm4che3.net.pdu.AAssociateRQ;
import org.dcm4che3.net.pdu.PresentationContext;
import org.dcm4che3.net.service.BasicCEchoSCP;
import org.dcm4che3.net.service.BasicCStoreSCP;
import org.dcm4che3.net.service.DicomServiceRegistry;

import java.io.IOException;
import java.net.ServerSocket;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Stand-in for "gateway + AI": receives studies and, shortly after the last instance of a
 * study, C-STOREs one AI result (Secondary Capture, new series, same study) to the tester.
 */
final class FakeAi implements AutoCloseable {

    final int port;
    final List<Attributes> received = new CopyOnWriteArrayList<>();
    volatile boolean respond = true;
    /** Answer with this StudyInstanceUID instead of the received one (simulates pseudonymized results). */
    volatile String answerStudyUid;
    private final int replyPort;
    private final Device device = new Device("fake-ai");
    private final ExecutorService exec = Executors.newCachedThreadPool();
    private final ScheduledExecutorService sched = Executors.newScheduledThreadPool(2);
    private final Map<String, ScheduledFuture<?>> pending = new ConcurrentHashMap<>();
    private final Map<String, Attributes> lastByStudy = new ConcurrentHashMap<>();

    FakeAi(String aet, int replyPort) throws Exception {
        this.port = freePort();
        this.replyPort = replyPort;
        ApplicationEntity ae = new ApplicationEntity(aet);
        ae.addTransferCapability(new TransferCapability(null, "*", TransferCapability.Role.SCP, "*"));
        Connection conn = new Connection(null, "127.0.0.1", port);
        DicomServiceRegistry reg = new DicomServiceRegistry();
        reg.addDicomService(new BasicCEchoSCP());
        reg.addDicomService(new BasicCStoreSCP("*") {
            @Override
            protected void store(Association as, PresentationContext pc, Attributes rq, PDVInputStream data,
                                 Attributes rsp) throws IOException {
                Attributes ds = data.readDataset(pc.getTransferSyntax());
                received.add(ds);
                String study = ds.getString(Tag.StudyInstanceUID);
                lastByStudy.put(study, ds);
                ScheduledFuture<?> old = pending.put(study, sched.schedule(() -> reply(study), 400, TimeUnit.MILLISECONDS));
                if (old != null) old.cancel(false);
            }
        });
        ae.setDimseRQHandler(reg);
        device.addApplicationEntity(ae);
        device.addConnection(conn);
        ae.addConnection(conn);
        device.setExecutor(exec);
        device.setScheduledExecutor(sched);
        device.bindConnections();
    }

    private void reply(String study) {
        if (!respond) return;
        Attributes src = lastByStudy.get(study);
        Attributes r = new Attributes();
        r.setString(Tag.SOPClassUID, VR.UI, UID.SecondaryCaptureImageStorage);
        r.setString(Tag.SOPInstanceUID, VR.UI, Ids.newUid());
        r.setString(Tag.StudyInstanceUID, VR.UI, answerStudyUid != null ? answerStudyUid : study);
        r.setString(Tag.SeriesInstanceUID, VR.UI, Ids.newUid());
        r.setString(Tag.AccessionNumber, VR.SH, answerStudyUid != null ? "" : src.getString(Tag.AccessionNumber, ""));
        r.setString(Tag.Modality, VR.CS, "SC");
        r.setString(Tag.SeriesDescription, VR.LO, "AI result");
        Device d = new Device("fake-ai-scu");
        ExecutorService e = Executors.newCachedThreadPool();
        ScheduledExecutorService s = Executors.newSingleThreadScheduledExecutor();
        d.setExecutor(e);
        d.setScheduledExecutor(s);
        try {
            Connection local = new Connection();
            ApplicationEntity ae = new ApplicationEntity("FAKEAI");
            ae.addConnection(local);
            d.addConnection(local);
            d.addApplicationEntity(ae);
            AAssociateRQ rq = new AAssociateRQ();
            rq.setCallingAET("FAKEAI");
            rq.setCalledAET("AIFITEST");
            rq.addPresentationContext(new PresentationContext(1, UID.SecondaryCaptureImageStorage, UID.ExplicitVRLittleEndian));
            Association as = ae.connect(local, new Connection(null, "127.0.0.1", replyPort), rq);
            as.cstore(UID.SecondaryCaptureImageStorage, r.getString(Tag.SOPInstanceUID), Priority.NORMAL,
                    new DataWriterAdapter(r), UID.ExplicitVRLittleEndian, new DimseRSPHandler(as.nextMessageID()));
            as.waitForOutstandingRSP();
            as.release();
            as.waitForSocketClose();
        } catch (Exception ex) {
            throw new IllegalStateException(ex);
        } finally {
            e.shutdownNow();
            s.shutdownNow();
        }
    }

    static int freePort() throws IOException {
        try (ServerSocket s = new ServerSocket(0)) { return s.getLocalPort(); }
    }

    @Override
    public void close() {
        device.unbindConnections();
        exec.shutdownNow();
        sched.shutdownNow();
    }
}

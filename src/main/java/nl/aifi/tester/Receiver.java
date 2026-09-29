package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Tag;
import org.dcm4che3.io.DicomOutputStream;
import org.dcm4che3.net.ApplicationEntity;
import org.dcm4che3.net.Association;
import org.dcm4che3.net.Connection;
import org.dcm4che3.net.Device;
import org.dcm4che3.net.PDVInputStream;
import org.dcm4che3.net.TransferCapability;
import org.dcm4che3.net.pdu.PresentationContext;
import org.dcm4che3.net.service.BasicCEchoSCP;
import org.dcm4che3.net.service.BasicCStoreSCP;
import org.dcm4che3.net.service.DicomServiceRegistry;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.logging.Logger;

/** C-STORE listener for the AI results. Every received object is handed to the {@link Tracker}. */
public final class Receiver implements AutoCloseable {

    private static final Logger LOG = Logger.getLogger(Receiver.class.getName());

    private final TesterConfig.Receiver cfg;
    private final Tracker tracker;
    private final Path saveDir;
    private final Device device = new Device("aifi-tester-receiver");
    private final ExecutorService exec = Executors.newCachedThreadPool();
    private final ScheduledExecutorService sched = Executors.newSingleThreadScheduledExecutor();

    /** @param saveDir where received files are kept, or null */
    public Receiver(TesterConfig.Receiver cfg, Tracker tracker, Path saveDir) {
        this.cfg = cfg;
        this.tracker = tracker;
        this.saveDir = saveDir;
    }

    public void start() throws Exception {
        ApplicationEntity ae = new ApplicationEntity(cfg.aeTitle);
        ae.addTransferCapability(new TransferCapability(null, "*", TransferCapability.Role.SCP, "*"));
        Connection conn = new Connection();
        conn.setBindAddress(cfg.bindAddress);
        conn.setPort(cfg.port);
        TlsSupport.apply(device, conn, cfg.tls, true);
        DicomServiceRegistry reg = new DicomServiceRegistry();
        reg.addDicomService(new BasicCEchoSCP());
        reg.addDicomService(new BasicCStoreSCP("*") {
            @Override
            protected void store(Association as, PresentationContext pc, Attributes rq, PDVInputStream data,
                                 Attributes rsp) throws IOException {
                Attributes ds = data.readDataset(pc.getTransferSyntax());
                if (saveDir != null) save(as, pc, rq, ds);
                tracker.onReceived(ds, as.getCallingAET());
            }
        });
        ae.setDimseRQHandler(reg);
        device.addApplicationEntity(ae);
        device.addConnection(conn);
        ae.addConnection(conn);
        device.setExecutor(exec);
        device.setScheduledExecutor(sched);
        device.bindConnections();
        LOG.info("Result receiver listening: AE " + cfg.aeTitle + " " + cfg.bindAddress + ":" + cfg.port
                + (cfg.tls.enabled ? " (TLS)" : ""));
    }

    private void save(Association as, PresentationContext pc, Attributes rq, Attributes ds) throws IOException {
        String study = ds.getString(Tag.StudyInstanceUID, "unknown");
        String sop = rq.getString(Tag.AffectedSOPInstanceUID);
        Path dir = saveDir.resolve(study.replaceAll("[^0-9.]", "_"));
        Files.createDirectories(dir);
        Attributes fmi = as.createFileMetaInformation(sop, rq.getString(Tag.AffectedSOPClassUID), pc.getTransferSyntax());
        try (DicomOutputStream out = new DicomOutputStream(dir.resolve(sop.replaceAll("[^0-9.]", "_") + ".dcm").toFile())) {
            out.writeDataset(fmi, ds);
        }
    }

    @Override
    public void close() {
        device.unbindConnections();
        exec.shutdownNow();
        sched.shutdownNow();
    }
}

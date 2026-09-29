package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Tag;
import org.dcm4che3.io.DicomInputStream;
import org.dcm4che3.io.DicomOutputStream;
import org.dcm4che3.net.Status;
import org.dcm4che3.net.service.DicomServiceException;
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

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
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
    private final Path unreadableDir;
    private final Device device = new Device("aifi-tester-receiver");
    private final ExecutorService exec = Executors.newCachedThreadPool();
    private final ScheduledExecutorService sched = Executors.newSingleThreadScheduledExecutor();

    /**
     * @param saveDir       where received files are kept, or null
     * @param unreadableDir where objects that are not valid DICOM are kept for analysis
     */
    public Receiver(TesterConfig.Receiver cfg, Tracker tracker, Path saveDir, Path unreadableDir) {
        this.cfg = cfg;
        this.tracker = tracker;
        this.saveDir = saveDir;
        this.unreadableDir = unreadableDir;
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
                ByteArrayOutputStream raw = new ByteArrayOutputStream();
                data.copyTo(raw);
                byte[] bytes = raw.toByteArray();
                Attributes ds;
                try (DicomInputStream in = new DicomInputStream(new ByteArrayInputStream(bytes), pc.getTransferSyntax())) {
                    ds = in.readDataset();
                } catch (IOException | RuntimeException e) {
                    unreadable(as, pc, rq, bytes, e);
                    throw new DicomServiceException(Status.ProcessingFailure, "Not a readable DICOM data set");
                }
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

    /** Log who sent an unreadable object and what it looks like, and keep the bytes. */
    private void unreadable(Association as, PresentationContext pc, Attributes rq, byte[] bytes, Exception e) {
        String kind = describe(bytes);
        String file = "";
        try {
            Files.createDirectories(unreadableDir);
            String sop = rq.getString(Tag.AffectedSOPInstanceUID, "unknown").replaceAll("[^0-9.]", "_");
            Path f = unreadableDir.resolve(sop + ".bin");
            Files.write(f, bytes);
            file = f.toString();
        } catch (IOException io) {
            LOG.warning("Cannot keep the unreadable object: " + io.getMessage());
        }
        LOG.warning("Unreadable object from AE " + as.getCallingAET() + " (" + as.getSocket().getInetAddress().getHostAddress()
                + "), SOP class " + rq.getString(Tag.AffectedSOPClassUID) + ", transfer syntax " + pc.getTransferSyntax()
                + ", " + bytes.length + " bytes: " + kind + " (" + e.getClass().getSimpleName() + ")"
                + (file.isEmpty() ? "" : "; kept as " + file) + ". Answered with C-STORE status 0110 (processing failure).");
        tracker.onUnreadable(as.getCallingAET(), rq.getString(Tag.AffectedSOPClassUID, ""), kind);
    }

    /** What an unreadable payload looks like, from its first bytes. */
    static String describe(byte[] b) {
        if (b.length >= 4 && b[0] == 'P' && b[1] == 'K' && b[2] == 3 && b[3] == 4) {
            return "a ZIP file instead of a DICOM data set";
        }
        if (b.length >= 4 && b[0] == '%' && b[1] == 'P' && b[2] == 'D' && b[3] == 'F') {
            return "a PDF file instead of a DICOM data set";
        }
        if (b.length >= 132 && b[128] == 'D' && b[129] == 'I' && b[130] == 'C' && b[131] == 'M') {
            return "a complete DICOM file (preamble + DICM + meta header) sent as data set - a bug in the sender";
        }
        if (b.length == 0) return "an empty data set";
        StringBuilder hex = new StringBuilder("not a DICOM data set, starts with");
        for (int i = 0; i < Math.min(8, b.length); i++) hex.append(String.format(" %02X", b[i]));
        return hex.toString();
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

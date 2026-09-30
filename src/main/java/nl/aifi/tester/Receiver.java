package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Tag;
import org.dcm4che3.data.UID;
import org.dcm4che3.io.DicomInputStream;
import org.dcm4che3.io.DicomOutputStream;
import org.dcm4che3.net.Status;
import org.dcm4che3.net.service.DicomServiceException;
import org.dcm4che3.net.ApplicationEntity;
import org.dcm4che3.net.Association;
import org.dcm4che3.net.AssociationHandler;
import org.dcm4che3.net.Connection;
import org.dcm4che3.net.Device;
import org.dcm4che3.net.PDVInputStream;
import org.dcm4che3.net.TransferCapability;
import org.dcm4che3.net.pdu.AAssociateAC;
import org.dcm4che3.net.pdu.AAssociateRQ;
import org.dcm4che3.net.pdu.PresentationContext;
import org.dcm4che3.net.pdu.UserIdentityAC;
import org.dcm4che3.net.service.BasicCEchoSCP;
import org.dcm4che3.net.service.BasicCStoreSCP;
import org.dcm4che3.net.service.DicomServiceRegistry;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.FilterInputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.Locale;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.logging.Logger;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** C-STORE listener for the AI results. Every received object is handed to the {@link Tracker}. */
public final class Receiver implements AutoCloseable {

    private static final Logger LOG = Logger.getLogger(Receiver.class.getName());

    /**
     * The standard transfer syntaxes the receiver can read. A private one (for example a vendor
     * syntax that ZIP-compresses the data set) is refused during association negotiation, so the
     * sender falls back to a standard syntax it also proposed - as a PACS would make it do.
     */
    static final String[] TRANSFER_SYNTAXES = {
        UID.ImplicitVRLittleEndian, UID.ExplicitVRLittleEndian, UID.EncapsulatedUncompressedExplicitVRLittleEndian,
        UID.DeflatedExplicitVRLittleEndian, UID.ExplicitVRBigEndian,
        UID.JPEGBaseline8Bit, UID.JPEGExtended12Bit, UID.JPEGExtended35, UID.JPEGSpectralSelectionNonHierarchical68,
        UID.JPEGSpectralSelectionNonHierarchical79, UID.JPEGFullProgressionNonHierarchical1012,
        UID.JPEGFullProgressionNonHierarchical1113, UID.JPEGLossless, UID.JPEGLosslessNonHierarchical15,
        UID.JPEGExtendedHierarchical1618, UID.JPEGExtendedHierarchical1719, UID.JPEGSpectralSelectionHierarchical2022,
        UID.JPEGSpectralSelectionHierarchical2123, UID.JPEGFullProgressionHierarchical2426,
        UID.JPEGFullProgressionHierarchical2527, UID.JPEGLosslessHierarchical28, UID.JPEGLosslessHierarchical29,
        UID.JPEGLosslessSV1, UID.JPEGLSLossless, UID.JPEGLSNearLossless,
        UID.JPEG2000Lossless, UID.JPEG2000, UID.JPEG2000MCLossless, UID.JPEG2000MC,
        UID.JPIPReferenced, UID.JPIPReferencedDeflate,
        UID.MPEG2MPML, UID.MPEG2MPMLF, UID.MPEG2MPHL, UID.MPEG2MPHLF, UID.MPEG4HP41, UID.MPEG4HP41F,
        UID.MPEG4HP41BD, UID.MPEG4HP41BDF, UID.MPEG4HP422D, UID.MPEG4HP422DF, UID.MPEG4HP423D, UID.MPEG4HP423DF,
        UID.MPEG4HP42STEREO, UID.MPEG4HP42STEREOF, UID.HEVCMP51, UID.HEVCM10P51,
        UID.HTJ2KLossless, UID.HTJ2KLosslessRPCL, UID.HTJ2K, UID.JPIPHTJ2KReferenced, UID.JPIPHTJ2KReferencedDeflate,
        UID.RLELossless
    };

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
        ae.addTransferCapability(new TransferCapability(null, "*", TransferCapability.Role.SCP, TRANSFER_SYNTAXES));
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
                if (isZip(bytes)) {
                    if (unzip(as, pc, rq, bytes)) return;
                    unreadable(as, pc, rq, bytes, "no DICOM object inside");
                    throw new DicomServiceException(Status.ProcessingFailure, "A ZIP file, not a DICOM data set");
                }
                Attributes ds;
                try (DicomInputStream in = new DicomInputStream(new ByteArrayInputStream(bytes), pc.getTransferSyntax())) {
                    ds = in.readDataset();
                } catch (IOException | RuntimeException e) {
                    unreadable(as, pc, rq, bytes, e.getClass().getSimpleName());
                    throw new DicomServiceException(Status.ProcessingFailure, "Not a readable DICOM data set");
                }
                if (saveDir != null) {
                    save(as, rq.getString(Tag.AffectedSOPClassUID), rq.getString(Tag.AffectedSOPInstanceUID),
                            pc.getTransferSyntax(), ds);
                }
                tracker.onReceived(ds, as.getCallingAET(), false);
            }
        });
        ae.setDimseRQHandler(reg);
        device.addApplicationEntity(ae);
        device.addConnection(conn);
        ae.addConnection(conn);
        device.setAssociationHandler(new RefusalLogger());
        device.setExecutor(exec);
        device.setScheduledExecutor(sched);
        device.bindConnections();
        LOG.info("Result receiver listening: AE " + cfg.aeTitle + " " + cfg.bindAddress + ":" + cfg.port
                + (cfg.tls.enabled ? " (TLS)" : ""));
    }

    /** Log who sent an unreadable object and what it looks like, and keep the bytes. */
    private void unreadable(Association as, PresentationContext pc, Attributes rq, byte[] bytes, String cause) {
        String kind = describe(bytes);
        String file = keep(rq, bytes, ".bin");
        LOG.warning("Unreadable object from AE " + as.getCallingAET() + " (" + as.getSocket().getInetAddress().getHostAddress()
                + "), SOP class " + rq.getString(Tag.AffectedSOPClassUID) + ", transfer syntax " + tsName(pc.getTransferSyntax())
                + ", " + bytes.length + " bytes: " + kind + " (" + cause + ")"
                + (file.isEmpty() ? "" : "; kept as " + file) + ". Answered with C-STORE status 0110 (processing failure).");
        tracker.onUnreadable(as.getCallingAET(), rq.getString(Tag.AffectedSOPClassUID, ""), kind, "status 0110 (geweigerd)");
    }

    static boolean isZip(byte[] b) {
        return b.length >= 4 && b[0] == 'P' && b[1] == 'K' && b[2] == 3 && b[3] == 4;
    }

    /**
     * A ZIP file sent as data set: read the DICOM files inside and count them as received objects.
     * Returns false (so the object is handled as unreadable) when the ZIP holds no DICOM object.
     */
    private boolean unzip(Association as, PresentationContext pc, Attributes rq, byte[] bytes) {
        int dicom = 0;
        Set<String> other = new TreeSet<>();
        try (ZipInputStream zin = new ZipInputStream(new ByteArrayInputStream(bytes))) {
            for (ZipEntry e; (e = zin.getNextEntry()) != null; ) {
                if (e.isDirectory()) continue;
                Attributes ds = null;
                String ts = null;
                try (DicomInputStream in = new DicomInputStream(new FilterInputStream(zin) {
                    @Override
                    public void close() {           // the next entry is read from the same stream
                    }
                })) {
                    ds = in.readDataset();
                    ts = in.getTransferSyntax();
                } catch (IOException | RuntimeException notDicom) {
                    ds = null;
                }
                String sop = ds == null ? null : ds.getString(Tag.SOPInstanceUID);
                if (sop == null) {
                    String name = e.getName();
                    int dot = name.lastIndexOf('.');
                    other.add(dot < 0 || dot < name.lastIndexOf('/') ? "(no extension)" : name.substring(dot).toLowerCase(Locale.ROOT));
                    continue;
                }
                dicom++;
                if (saveDir != null) {
                    try {
                        save(as, ds.getString(Tag.SOPClassUID), sop, ts, ds);
                    } catch (IOException io) {
                        LOG.warning("Cannot save an object unpacked from a ZIP: " + io.getMessage());
                    }
                }
                tracker.onReceived(ds, as.getCallingAET(), true);
            }
        } catch (IOException | RuntimeException e) {
            if (dicom == 0) return false;
            LOG.warning("ZIP from AE " + as.getCallingAET() + " is damaged after " + dicom + " DICOM object(s): " + e);
        }
        if (dicom == 0) return false;
        String file = saveDir != null ? keep(rq, bytes, ".zip") : "";        // holds the result: kept like received files
        String kind = "a ZIP file with " + dicom + " DICOM object(s)"
                + (other.isEmpty() ? "" : " and other files (" + String.join(", ", other) + ")") + " instead of a DICOM data set";
        LOG.warning("AE " + as.getCallingAET() + " (" + as.getSocket().getInetAddress().getHostAddress() + ") sent a ZIP file"
                + " instead of a DICOM data set (SOP class " + rq.getString(Tag.AffectedSOPClassUID) + ", transfer syntax "
                + tsName(pc.getTransferSyntax()) + ", " + bytes.length + " bytes). Unpacked " + dicom + " DICOM object(s)"
                + (other.isEmpty() ? "" : " and skipped other file(s) " + other) + "; they count as received"
                + (file.isEmpty() ? "" : ", ZIP kept as " + file) + ". A PACS such as JiveX refuses this: the sender must send"
                + " plain DICOM (no ZIP, no private transfer syntax).");
        tracker.onUnreadable(as.getCallingAET(), rq.getString(Tag.AffectedSOPClassUID, ""), kind,
                "uitgepakt en meegeteld, status 0000");
        return true;
    }

    /** Keep the raw bytes of an odd object for analysis; returns the file name or "". */
    private String keep(Attributes rq, byte[] bytes, String ext) {
        try {
            Files.createDirectories(unreadableDir);
            String sop = rq.getString(Tag.AffectedSOPInstanceUID, "unknown").replaceAll("[^0-9.]", "_");
            Path f = unreadableDir.resolve(sop + ext);
            Files.write(f, bytes);
            return f.toString();
        } catch (IOException io) {
            LOG.warning("Cannot keep the object: " + io.getMessage());
            return "";
        }
    }

    static String tsName(String ts) {
        String name = UID.nameOf(ts);
        return ts + (name == null || name.equals("?") ? " (private / unknown)" : " (" + name + ")");
    }

    /**
     * Logs presentation contexts refused because of their transfer syntax, so a sender that offers
     * only a private syntax is visible in the log instead of just "no results".
     */
    private static final class RefusalLogger extends AssociationHandler {
        @Override
        protected AAssociateAC makeAAssociateAC(Association as, AAssociateRQ rq, UserIdentityAC userIdentity) throws IOException {
            AAssociateAC ac = super.makeAAssociateAC(as, rq, userIdentity);
            for (PresentationContext rqpc : rq.getPresentationContexts()) {
                PresentationContext acpc = ac.getPresentationContext(rqpc.getPCID());
                if (acpc == null || acpc.getResult() != PresentationContext.TRANSFER_SYNTAX_NOT_SUPPORTED) continue;
                boolean alternative = ac.getPresentationContexts().stream().anyMatch(p -> p.isAccepted()
                        && rqpc.getAbstractSyntax().equals(rq.getPresentationContext(p.getPCID()).getAbstractSyntax()));
                String msg = "AE " + rq.getCallingAET() + " proposed SOP class " + rqpc.getAbstractSyntax()
                        + " with transfer syntax(es) the tester does not accept: " + Arrays.toString(rqpc.getTransferSyntaxes());
                if (alternative) {
                    LOG.info(msg + " - refused; the sender also offered a standard transfer syntax, which is used.");
                } else {
                    LOG.warning(msg + " - refused and no standard transfer syntax offered for this SOP class, so these objects"
                            + " cannot be sent. Set the sender to Explicit or Implicit VR Little Endian.");
                }
            }
            return ac;
        }
    }

    /** What an unreadable payload looks like, from its first bytes. */
    static String describe(byte[] b) {
        if (isZip(b)) {
            return "a ZIP file without DICOM objects instead of a DICOM data set";
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

    private void save(Association as, String sopClass, String sop, String ts, Attributes ds) throws IOException {
        String study = ds.getString(Tag.StudyInstanceUID, "unknown");
        Path dir = saveDir.resolve(study.replaceAll("[^0-9.]", "_"));
        Files.createDirectories(dir);
        Attributes fmi = as.createFileMetaInformation(sop, sopClass, ts);
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

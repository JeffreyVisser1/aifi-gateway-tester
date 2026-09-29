package nl.aifi.tester;

import org.dcm4che3.data.Attributes;
import org.dcm4che3.data.Tag;
import org.dcm4che3.io.DicomInputStream;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.logging.Logger;
import java.util.stream.Stream;

/**
 * The test studies: every DICOM file under the studies folder, grouped by
 * StudyInstanceUID. Only headers are read while scanning.
 */
public final class StudyLibrary {

    private static final Logger LOG = Logger.getLogger(StudyLibrary.class.getName());

    /** One study in the folder. {@code ref} identifies it in the report without patient data. */
    public static final class Study {
        public final String ref;
        public final String studyUid;
        public final String modality;
        public final List<Path> files;
        public final long bytes;

        Study(String ref, String studyUid, String modality, List<Path> files, long bytes) {
            this.ref = ref;
            this.studyUid = studyUid;
            this.modality = modality;
            this.files = files;
            this.bytes = bytes;
        }
    }

    private final List<Study> studies;
    private final int skippedFiles;

    private StudyLibrary(List<Study> studies, int skippedFiles) {
        this.studies = studies;
        this.skippedFiles = skippedFiles;
    }

    public List<Study> studies() { return studies; }
    public int skippedFiles() { return skippedFiles; }

    public Study random(Random rnd) {
        return studies.get(rnd.nextInt(studies.size()));
    }

    public static StudyLibrary scan(Path dir, List<String> modalities) throws IOException {
        if (!Files.isDirectory(dir)) throw new IOException("Studies folder not found: " + dir.toAbsolutePath());
        Map<String, List<Path>> byStudy = new LinkedHashMap<>();
        Map<String, String> modality = new LinkedHashMap<>();
        Map<String, Long> bytes = new LinkedHashMap<>();
        int skipped = 0;
        List<Path> files;
        try (Stream<Path> walk = Files.walk(dir)) {
            files = walk.filter(Files::isRegularFile).sorted().collect(java.util.stream.Collectors.toList());
        }
        for (Path f : files) {
            Attributes h;
            try (DicomInputStream in = new DicomInputStream(f.toFile())) {
                h = in.readDataset(-1, Tag.PixelData);
            } catch (Exception e) {
                skipped++;                               // not DICOM (README, DICOMDIR, ...)
                continue;
            }
            String uid = h.getString(Tag.StudyInstanceUID);
            if (uid == null || h.getString(Tag.SOPInstanceUID) == null || h.getString(Tag.SOPClassUID) == null) {
                skipped++;
                continue;
            }
            byStudy.computeIfAbsent(uid, k -> new ArrayList<>()).add(f);
            modality.putIfAbsent(uid, h.getString(Tag.Modality, ""));
            bytes.merge(uid, Files.size(f), Long::sum);
        }
        List<Study> out = new ArrayList<>();
        for (Map.Entry<String, List<Path>> e : byStudy.entrySet()) {
            String m = modality.get(e.getKey());
            if (!modalities.isEmpty() && !modalities.contains(m)) continue;
            out.add(new Study(Ids.ref(e.getKey()), e.getKey(), m, Collections.unmodifiableList(e.getValue()),
                    bytes.get(e.getKey())));
        }
        LOG.info("Studies folder " + dir.toAbsolutePath() + ": " + out.size() + " usable stud(ies)"
                + (modalities.isEmpty() ? "" : " with modality " + modalities)
                + ", " + (byStudy.size() - out.size()) + " other stud(ies), " + skipped + " non-DICOM file(s)");
        return new StudyLibrary(out, skipped);
    }
}

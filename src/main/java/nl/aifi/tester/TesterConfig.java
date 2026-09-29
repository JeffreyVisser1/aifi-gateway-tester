package nl.aifi.tester;

import org.yaml.snakeyaml.LoaderOptions;
import org.yaml.snakeyaml.Yaml;
import org.yaml.snakeyaml.constructor.SafeConstructor;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.lang.reflect.ParameterizedType;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** {@code tester.yaml}. Unknown keys are refused so a typo never silently becomes a default. */
public final class TesterConfig {

    public Studies studies = new Studies();
    public List<Target> targets = new ArrayList<>();
    public Receiver receiver = new Receiver();
    public Matching matching = new Matching();
    public JivexDatabase jivexDatabase = new JivexDatabase();
    public List<Schedule> schedules = new ArrayList<>();
    public Run run = new Run();
    public Report report = new Report();

    /** Folder with test studies (DICOM files, any sub-folder layout). */
    public static final class Studies {
        public String dir = "studies";
        /** Only studies with one of these modalities; empty = all. */
        public List<String> modalities = new ArrayList<>(List.of("CT"));
        /**
         * Give every send new Study/Series/SOP Instance UIDs and a new AccessionNumber, so
         * each test is a new study for JiveX, the gateway and the AI, and results can be
         * matched to exactly one test. Patient data stays as in the files.
         */
        public boolean uniquify = true;
        /** Prefix of the generated AccessionNumber (max 16 characters in total). */
        public String accessionPrefix = "AIFIT";
    }

    /** A gateway port (route) studies are sent to. */
    public static final class Target {
        public String name = "";
        public String host = "127.0.0.1";
        public int port = 11112;
        public String aeTitle = "AIFIGW";
        public String callingAeTitle = "AIFITEST";
        /** Relative share of the studies sent to this target. */
        public int weight = 1;
        public int connectTimeoutMs = 5_000;
        public int responseTimeoutMs = 60_000;
        public Tls tls = new Tls();
    }

    /** Where the AI results come back: a C-STORE listener in this program. */
    public static final class Receiver {
        public String bindAddress = "0.0.0.0";
        public int port = 11200;
        public String aeTitle = "AIFITEST";
        /** Keep received result files under report/&lt;session&gt;/received (false = headers only). */
        public boolean saveFiles = false;
        public Tls tls = new Tls();
    }

    public static final class Tls {
        public boolean enabled = false;
        /** PKCS#12 with this program's certificate + key. */
        public String keystorePath = "";
        public String keystorePassword = "";
        /** PEM with the trusted peer certificate(s) or CA. */
        public String trustCertPath = "";
        /** Receiver only: require a client certificate. */
        public boolean requireClientCert = false;
        /** Sender only: the gateway certificate must name the host. */
        public boolean verifyHostname = true;
    }

    public static final class Matching {
        /** A test fails when no result arrives within this time. */
        public int resultTimeoutMinutes = 30;
        /** After the first result instance, wait this long for the rest before closing the test. */
        public int settleSeconds = 60;
        /** Match received objects by StudyInstanceUID. */
        public boolean byStudyInstanceUid = true;
        /** Match received objects by AccessionNumber. */
        public boolean byAccessionNumber = true;
        /** Only these modalities count as an AI result (e.g. [SR, SEG, SC]); empty = any. */
        public List<String> resultModalities = new ArrayList<>();
        /** Only series whose SeriesDescription contains this text count (empty = any). */
        public String resultSeriesDescriptionContains = "";
    }

    /**
     * Optional read-only access to the JiveX MariaDB: the pseudonymized StudyInstanceUID and
     * PatientID of every sent study are then also used to match results that come back
     * pseudonymized.
     */
    public static final class JivexDatabase {
        public boolean enabled = false;
        public String host = "127.0.0.1";
        public int port = 3306;
        public String name = "jivex";
        public String user = "";
        public String password = "";
        public String table = "jivepseudodicomdata";
        public String newStudyUidColumn = "newStudyInstanceUID";
        public String newPatientIdColumn = "newPatientID";
        public String studyJoinColumn = "jiveStudyDataID";
        public String studyJoinTable = "jivestudydata";
        public String studyJoinTableKey = "jiveStudyDataID";
        public String originalStudyUidColumn = "studyInstanceUID";
        public String orderByColumn = "lastUpdateDateTime";
    }

    /** "count studies every everyMinutes minutes"; count > 1 is a burst. */
    public static final class Schedule {
        public String name = "";
        public int everyMinutes = 5;
        public int count = 1;
        /** Delay before the first round. */
        public int startAfterMinutes = 0;
        /** Only this target (name); empty = random over all targets by weight. */
        public String target = "";
    }

    public static final class Run {
        /** Stop sending after this many hours (0 = until stopped with Ctrl+C). */
        public double durationHours = 0;
        /** Sends running at the same time (a burst of 10 with 4 parallel = 4 associations at once). */
        public int parallelSends = 4;
    }

    public static final class Report {
        public String dir = "reports";
        public int refreshSeconds = 30;
    }

    // ── loading ──────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    public static TesterConfig load(Path file) throws Exception {
        LoaderOptions opts = new LoaderOptions();
        opts.setAllowDuplicateKeys(false);
        Object root = new Yaml(new SafeConstructor(opts)).load(Files.readString(file));
        return fromMap(root == null ? Map.of() : (Map<String, Object>) root);
    }

    public static TesterConfig fromMap(Map<String, Object> yaml) {
        TesterConfig c = new TesterConfig();
        bind(c, yaml, "");
        return c;
    }

    @SuppressWarnings("unchecked")
    private static void bind(Object target, Map<String, Object> map, String prefix) {
        for (Map.Entry<String, Object> e : map.entrySet()) {
            String path = prefix + e.getKey();
            Field f;
            try {
                f = target.getClass().getField(e.getKey());
            } catch (NoSuchFieldException ex) {
                throw new IllegalArgumentException("Unknown setting '" + path + "'");
            }
            if (Modifier.isStatic(f.getModifiers())) throw new IllegalArgumentException("Unknown setting '" + path + "'");
            Object v = e.getValue();
            Class<?> t = f.getType();
            try {
                if (t == String.class) {
                    if (v instanceof Map || v instanceof List) throw new IllegalArgumentException("'" + path + "' must be a single value");
                    f.set(target, v == null ? "" : String.valueOf(v).trim());
                } else if (t == int.class) {
                    f.setInt(target, (int) number(v, path));
                } else if (t == double.class) {
                    f.setDouble(target, v instanceof Number ? ((Number) v).doubleValue() : number(v, path));
                } else if (t == boolean.class) {
                    if (!(v instanceof Boolean)) throw new IllegalArgumentException("'" + path + "' must be true or false");
                    f.setBoolean(target, (Boolean) v);
                } else if (t == List.class) {
                    Class<?> elem = (Class<?>) ((ParameterizedType) f.getGenericType()).getActualTypeArguments()[0];
                    List<Object> out = new ArrayList<>();
                    if (v instanceof List) {
                        int i = 0;
                        for (Object o : (List<Object>) v) {
                            String itemPath = path + "." + i++;
                            if (o == null) continue;
                            if (elem == String.class) {
                                out.add(String.valueOf(o).trim());
                            } else {
                                if (!(o instanceof Map)) throw new IllegalArgumentException("'" + itemPath + "' must be a section");
                                Object item = elem.getDeclaredConstructor().newInstance();
                                bind(item, (Map<String, Object>) o, itemPath + ".");
                                out.add(item);
                            }
                        }
                    } else if (v != null) {
                        throw new IllegalArgumentException("'" + path + "' must be a list");
                    }
                    f.set(target, out);
                } else {
                    if (v == null) continue;
                    if (!(v instanceof Map)) throw new IllegalArgumentException("'" + path + "' must be a section");
                    bind(f.get(target), (Map<String, Object>) v, path + ".");
                }
            } catch (ReflectiveOperationException ex) {
                throw new IllegalStateException(ex);
            }
        }
    }

    private static long number(Object v, String path) {
        if (v instanceof Number) return ((Number) v).longValue();
        try {
            return Long.parseLong(String.valueOf(v).trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("'" + path + "' must be a whole number, got '" + v + "'");
        }
    }

    /** Everything that must be fixed before a test run. */
    public List<String> errors() {
        List<String> e = new ArrayList<>();
        if (targets.isEmpty()) e.add("targets: at least one gateway port is required");
        Set<String> names = new HashSet<>();
        for (int i = 0; i < targets.size(); i++) {
            Target t = targets.get(i);
            String p = "targets." + i;
            if (t.name.isBlank()) e.add(p + ".name is required");
            else if (!names.add(t.name)) e.add(p + ".name '" + t.name + "' is used twice");
            if (t.host.isBlank()) e.add(p + ".host is required");
            if (t.port < 1 || t.port > 65535) e.add(p + ".port must be 1..65535");
            if (t.weight < 0) e.add(p + ".weight must be 0 or more");
        }
        if (!targets.isEmpty() && targets.stream().allMatch(t -> t.weight == 0)) e.add("targets: at least one weight must be above 0");
        if (receiver.port < 1 || receiver.port > 65535) e.add("receiver.port must be 1..65535");
        if (schedules.isEmpty()) e.add("schedules: at least one schedule is required");
        for (int i = 0; i < schedules.size(); i++) {
            Schedule s = schedules.get(i);
            String p = "schedules." + i;
            if (s.everyMinutes < 1) e.add(p + ".everyMinutes must be at least 1");
            if (s.count < 1) e.add(p + ".count must be at least 1");
            if (!s.target.isBlank() && !names.contains(s.target)) e.add(p + ".target '" + s.target + "' is not a target name");
        }
        if (matching.resultTimeoutMinutes < 1) e.add("matching.resultTimeoutMinutes must be at least 1");
        if (!matching.byStudyInstanceUid && !matching.byAccessionNumber && !jivexDatabase.enabled) {
            e.add("matching: enable byStudyInstanceUid, byAccessionNumber or jivexDatabase");
        }
        if (studies.accessionPrefix.length() > 8) e.add("studies.accessionPrefix may be at most 8 characters");
        if (jivexDatabase.enabled && jivexDatabase.user.isBlank()) e.add("jivexDatabase.user is required");
        if (run.parallelSends < 1) e.add("run.parallelSends must be at least 1");
        return e;
    }
}

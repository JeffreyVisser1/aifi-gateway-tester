package nl.aifi.tester;

import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.CountDownLatch;
import java.util.logging.ConsoleHandler;
import java.util.logging.FileHandler;
import java.util.logging.Formatter;
import java.util.logging.Handler;
import java.util.logging.Level;
import java.util.logging.LogManager;
import java.util.logging.LogRecord;
import java.util.logging.Logger;

/**
 * <pre>
 *   aifi-gateway-tester run                 schedules from tester.yaml (bursts and intervals)
 *   aifi-gateway-tester burst &lt;n&gt; [--target &lt;name&gt;]   send n studies now, wait for the results
 *   aifi-gateway-tester scan                list the usable studies in the studies folder
 *   aifi-gateway-tester echo                C-ECHO every target
 *   options: --config &lt;file&gt;   default: tester.yaml
 * </pre>
 */
public final class Main {

    private static final Logger LOG = Logger.getLogger(Main.class.getName());

    private Main() {}

    public static void main(String[] args) throws Exception {
        String command = args.length > 0 && !args[0].startsWith("--") ? args[0] : "run";
        Path configFile = Path.of("tester.yaml");
        String target = "";
        int count = 1;
        for (int i = 0; i < args.length; i++) {
            if ("--config".equals(args[i]) && i + 1 < args.length) configFile = Path.of(args[++i]);
            else if ("--target".equals(args[i]) && i + 1 < args.length) target = args[++i];
            else if (i == 1 && "burst".equals(command)) count = Integer.parseInt(args[i]);
        }
        logging(null);
        if ("version".equals(command)) {
            System.out.println("AIFI Gateway Tester " + version());
            return;
        }
        if (!Files.exists(configFile)) {
            try (InputStream in = Main.class.getResourceAsStream("/default-tester.yaml")) {
                Files.copy(in, configFile);
            }
            LOG.warning("Created " + configFile.toAbsolutePath() + " - fill it in (see docs/HANDLEIDING.md) and start again");
            System.exit(2);
        }
        TesterConfig cfg;
        try {
            cfg = TesterConfig.load(configFile);
        } catch (IllegalArgumentException e) {
            LOG.severe("Configuration error in " + configFile + ": " + e.getMessage());
            System.exit(2);
            return;
        }
        List<String> errors = cfg.errors();
        if ("scan".equals(command)) errors.clear();
        for (String e : errors) LOG.severe("Configuration: " + e);
        if (!errors.isEmpty()) System.exit(2);

        switch (command) {
            case "scan": {
                StudyLibrary lib = StudyLibrary.scan(Path.of(cfg.studies.dir), cfg.studies.modalities);
                for (StudyLibrary.Study s : lib.studies()) {
                    System.out.printf("%s  %-4s %5d instance(s) %10.2f MB%n", s.ref, s.modality, s.files.size(), s.bytes / 1e6);
                }
                System.exit(lib.studies().isEmpty() ? 1 : 0);
                return;
            }
            case "echo": {
                boolean ok = true;
                for (TesterConfig.Target t : cfg.targets) {
                    String r = Sender.echo(t);
                    ok &= r.startsWith("OK");
                    System.out.println(t.name + "  " + t.aeTitle + "@" + t.host + ":" + t.port + "  " + r);
                }
                System.exit(ok ? 0 : 1);
                return;
            }
            case "run":
            case "burst":
                break;
            default:
                System.err.println("Unknown command '" + command + "'. Commands: run, burst <n> [--target name], scan, echo, version");
                System.exit(64);
                return;
        }
        String only = target;
        if (!only.isEmpty() && cfg.targets.stream().noneMatch(t -> t.name.equals(only))) {
            LOG.severe("No target named '" + target + "'");
            System.exit(2);
        }

        StudyLibrary lib = StudyLibrary.scan(Path.of(cfg.studies.dir), cfg.studies.modalities);
        if (lib.studies().isEmpty()) {
            LOG.severe("No usable studies in " + Path.of(cfg.studies.dir).toAbsolutePath());
            System.exit(1);
        }
        Path sessionDir = Path.of(cfg.report.dir).resolve(LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")));
        Files.createDirectories(sessionDir);
        logging(sessionDir.resolve("tester.log"));
        LOG.info("AIFI Gateway Tester " + version() + " - session " + sessionDir.getFileName());

        Tracker.PseudoLookup lookup = cfg.jivexDatabase.enabled ? new JivexLookup(cfg.jivexDatabase) : null;
        Session session = new Session(cfg, lib, sessionDir, lookup);
        session.start();
        long waitForResults = (cfg.matching.resultTimeoutMinutes * 60L + cfg.matching.settleSeconds + 60) * 1000L;
        CountDownLatch done = new CountDownLatch(1);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            session.close();
            done.countDown();
        }, "aifitest-shutdown"));

        if ("burst".equals(command)) {
            session.burst(count, target);
            session.awaitResults(waitForResults + count * 600_000L);
        } else {
            session.startSchedules();
            if (cfg.run.durationHours > 0) {
                Thread.sleep((long) (cfg.run.durationHours * 3_600_000L));
                LOG.info("Run duration reached; waiting for the last results");
                session.stopSending();
                session.awaitResults(waitForResults);
            } else {
                LOG.info("Running until stopped (Ctrl+C); the report is updated every " + cfg.report.refreshSeconds + " s");
                done.await();
                return;
            }
        }
        System.exit(0);   // runs the shutdown hook: final report
    }

    static void logging(Path file) throws Exception {
        if (file == null) {
            LogManager.getLogManager().reset();
            Handler h = new ConsoleHandler();
            h.setFormatter(new Line());
            h.setLevel(Level.ALL);
            Logger.getLogger("").addHandler(h);
            Logger.getLogger("").setLevel(Level.INFO);
            Logger.getLogger("org.dcm4che3").setLevel(Level.WARNING);
            Logger.getLogger("org.mariadb").setLevel(Level.WARNING);
            return;
        }
        FileHandler fh = new FileHandler(file.toString(), true);
        fh.setEncoding("UTF-8");
        fh.setFormatter(new Line());
        Logger.getLogger("").addHandler(fh);
    }

    static final class Line extends Formatter {
        @Override
        public String format(LogRecord r) {
            String name = r.getLoggerName() == null ? "" : r.getLoggerName();
            String s = String.format("%1$tF %1$tT %2$-7s %3$s: %4$s%n", r.getMillis(), r.getLevel().getName(),
                    name.substring(name.lastIndexOf('.') + 1), formatMessage(r));
            if (r.getThrown() != null) s += r.getThrown() + System.lineSeparator();
            return s;
        }
    }

    static String version() {
        try (InputStream in = Main.class.getResourceAsStream("version.properties")) {
            Properties p = new Properties();
            if (in != null) p.load(in);
            return p.getProperty("version", "dev");
        } catch (Exception e) {
            return "dev";
        }
    }
}

package nl.aifi.tester;

import java.nio.file.Path;
import java.security.SecureRandom;
import java.util.List;
import java.util.Random;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Level;
import java.util.logging.Logger;

/** One test session: schedules and bursts send studies, the receiver collects results, reports follow. */
public final class Session implements AutoCloseable {

    private static final Logger LOG = Logger.getLogger(Session.class.getName());
    static final long CHECK_SECONDS = Math.max(1, Long.getLong("aifitest.checkSeconds", 10));

    private final TesterConfig cfg;
    private final StudyLibrary library;
    private final Tracker tracker;
    private final Sender sender;
    private final ReportWriter report;
    private final Receiver receiver;
    private final Random rnd = new SecureRandom();
    private final ScheduledExecutorService timer = Executors.newScheduledThreadPool(2, r -> {
        Thread t = new Thread(r, "aifitest-timer");
        t.setDaemon(true);
        return t;
    });
    private final ExecutorService sendPool;
    private final AtomicInteger sendsInFlight = new AtomicInteger();
    private volatile boolean sending = true;

    public Session(TesterConfig cfg, StudyLibrary library, Path reportDir, Tracker.PseudoLookup lookup) {
        this.cfg = cfg;
        this.library = library;
        this.tracker = new Tracker(cfg.matching, lookup);
        this.sender = new Sender(cfg.studies);
        long now = System.currentTimeMillis();
        this.report = new ReportWriter(reportDir, cfg, now, library.studies().size());
        this.receiver = new Receiver(cfg.receiver, tracker, cfg.receiver.saveFiles ? reportDir.resolve("received") : null);
        this.sendPool = Executors.newFixedThreadPool(cfg.run.parallelSends, r -> {
            Thread t = new Thread(r, "aifitest-send");
            t.setDaemon(true);
            return t;
        });
    }

    public Tracker tracker() { return tracker; }
    public ReportWriter report() { return report; }

    public void start() throws Exception {
        receiver.start();
        timer.scheduleWithFixedDelay(this::checkSafely, CHECK_SECONDS, CHECK_SECONDS, TimeUnit.SECONDS);
        timer.scheduleWithFixedDelay(() -> writeReport("bezig"), 1, Math.max(5, cfg.report.refreshSeconds), TimeUnit.SECONDS);
        LOG.info("Report: " + report.dir().resolve("report.html").toAbsolutePath());
    }

    /** Start every configured schedule. */
    public void startSchedules() {
        for (TesterConfig.Schedule s : cfg.schedules) {
            timer.scheduleAtFixedRate(() -> {
                if (!sending) return;
                LOG.info("Schedule " + s.name + ": sending " + s.count + " stud(ies)");
                for (int i = 0; i < s.count; i++) submit(s.name, s.target);
            }, s.startAfterMinutes * 60L, s.everyMinutes * 60L, TimeUnit.SECONDS);
            LOG.info("Schedule " + s.name + ": " + s.count + " stud(ies) every " + s.everyMinutes + " min"
                    + (s.target.isEmpty() ? " to random ports" : " to " + s.target)
                    + (s.startAfterMinutes > 0 ? ", first after " + s.startAfterMinutes + " min" : ""));
        }
    }

    /** Send {@code n} studies right away (a manual burst). */
    public void burst(int n, String target) {
        LOG.info("Burst: sending " + n + " stud(ies)" + (target.isEmpty() ? " to random ports" : " to " + target));
        for (int i = 0; i < n; i++) submit("burst", target);
    }

    private void submit(String schedule, String targetName) {
        sendsInFlight.incrementAndGet();
        sendPool.submit(() -> {
            try {
                TesterConfig.Target target = pickTarget(targetName);
                StudyLibrary.Study study = library.random(rnd);
                TestRun run = tracker.newRun(schedule, target.name, study);
                sender.send(study, target, run);
                tracker.sent(run);
            } catch (RuntimeException e) {
                LOG.log(Level.SEVERE, "Send crashed", e);
            } finally {
                sendsInFlight.decrementAndGet();
            }
        });
    }

    TesterConfig.Target pickTarget(String name) {
        if (!name.isEmpty()) {
            for (TesterConfig.Target t : cfg.targets) if (t.name.equals(name)) return t;
            throw new IllegalArgumentException("No target named " + name);
        }
        int total = cfg.targets.stream().mapToInt(t -> t.weight).sum();
        int x = rnd.nextInt(total);
        for (TesterConfig.Target t : cfg.targets) {
            x -= t.weight;
            if (x < 0) return t;
        }
        return cfg.targets.get(0);
    }

    /** Stop sending new studies; results keep being collected. */
    public void stopSending() {
        sending = false;
    }

    /** Wait until every send finished and every test has an outcome, or {@code maxMs} passed. */
    public void awaitResults(long maxMs) throws InterruptedException {
        long end = System.currentTimeMillis() + maxMs;
        while (System.currentTimeMillis() < end && (sendsInFlight.get() > 0 || tracker.hasPending())) {
            Thread.sleep(500);
        }
    }

    private void checkSafely() {
        try {
            tracker.check(System.currentTimeMillis());
        } catch (Throwable e) {
            LOG.log(Level.WARNING, "Check failed", e);
        }
    }

    private void writeReport(String state) {
        try {
            report.write(tracker, state);
        } catch (Throwable e) {
            LOG.log(Level.WARNING, "Cannot write the report: " + e.getMessage(), e);
        }
    }

    /** Close open tests, write the final report and stop listening. */
    @Override
    public void close() {
        sending = false;
        timer.shutdownNow();
        sendPool.shutdown();
        try {
            sendPool.awaitTermination(60, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        tracker.check(System.currentTimeMillis());
        tracker.closeOpen(System.currentTimeMillis());
        writeReport("afgerond");
        receiver.close();
        List<TestRun> runs = tracker.runs();
        long pass = runs.stream().filter(r -> r.outcome == TestRun.Outcome.PASS).count();
        LOG.info("Session finished: " + runs.size() + " test(s), " + pass + " passed. Report: "
                + report.dir().resolve("report.html").toAbsolutePath());
    }
}

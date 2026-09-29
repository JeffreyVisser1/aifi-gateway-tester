package nl.aifi.tester;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.Properties;

/** Read-only lookup of the JiveX pseudonym of a sent study (same query as the gateway). */
final class JivexLookup implements Tracker.PseudoLookup {

    private final TesterConfig.JivexDatabase d;
    private final String sql;

    JivexLookup(TesterConfig.JivexDatabase d) {
        this.d = d;
        this.sql = "SELECT p." + q(d.newStudyUidColumn) + ", p." + q(d.newPatientIdColumn)
                + " FROM " + q(d.table) + " p"
                + " JOIN " + q(d.studyJoinTable) + " s ON s." + q(d.studyJoinTableKey) + " = p." + q(d.studyJoinColumn)
                + " WHERE s." + q(d.originalStudyUidColumn) + " = ?"
                + " ORDER BY p." + q(d.orderByColumn) + " DESC LIMIT 1";
    }

    @Override
    public String[] lookup(String sentStudyUid) throws Exception {
        Properties p = new Properties();
        p.setProperty("user", d.user);
        p.setProperty("password", d.password);
        p.setProperty("connectTimeout", "5000");
        p.setProperty("socketTimeout", "15000");
        try (Connection c = DriverManager.getConnection("jdbc:mariadb://" + d.host + ":" + d.port + "/" + q(d.name).replace("`", ""), p);
             PreparedStatement ps = c.prepareStatement(sql)) {
            c.setReadOnly(true);
            ps.setString(1, sentStudyUid);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? new String[] {rs.getString(1), rs.getString(2)} : null;
            }
        }
    }

    private static String q(String identifier) {
        if (identifier == null || !identifier.matches("[A-Za-z0-9_]{1,64}")) {
            throw new IllegalArgumentException("Invalid table/column name in jivexDatabase: '" + identifier + "'");
        }
        return "`" + identifier + "`";
    }
}

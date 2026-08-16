package com.xjtlu.energyagent.db

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase

/* ------------------------------------------------------------------ Entities */

@Entity(tableName = "device_info")
data class DeviceInfoEntity(
    @PrimaryKey val deviceId: String,
    val manufacturer: String?,
    val model: String?,
    val codename: String?,
    val androidVersion: String?,
    val sdkInt: Int?,
    val buildFingerprint: String?,
    val appVersion: String?,
    val batteryHealth: String?,
    val batteryCapacityUah: Int?
)

@Entity(tableName = "experiments")
data class ExperimentEntity(
    @PrimaryKey val experimentId: String,
    val environment: String?,
    val operatorName: String?,
    val notes: String?,
    val createdUtcMs: Long
)

@Entity(tableName = "sessions")
data class SessionEntity(
    @PrimaryKey val sessionId: String,
    val experimentId: String,
    val deviceId: String?,
    val startedUtcMs: Long?,
    val endedUtcMs: Long?
)

@Entity(tableName = "conditions")
data class ConditionEntity(
    @PrimaryKey val conditionId: String,
    val experimentId: String,
    val environment: String?,
    val orientationDeg: Double?,
    val incidentPowerDensityWm2: Double?,
    val stirrerMode: String?,
    val stirrerState: String?,
    val targetRsrpDbm: Double?,
    val trafficCondition: String?,
    val frequencyMhz: Double?,
    val bandwidthMhz: Double?,
    val txGainDb: Double?,
    val rxGainDb: Double?,
    val puschTargetSnrDb: Double?,
    val puschTargetSnrX10: Int?,
    val puschTargetMode: String?,
    val schedulerMode: String?,
    val mcs: Int?,
    val qm: Int?,
    val nPrb: Int?,
    val chamberMetadataJson: String?
)

@Entity(tableName = "runs")
data class RunEntity(
    @PrimaryKey val runId: String,
    val experimentId: String,
    val conditionId: String,
    val deviceId: String?,
    val sessionId: String?,
    val state: String,               // ARMED / RUNNING / COMPLETE / ABORTED
    val plannedStartUtcMs: Long?,
    val startDelaySeconds: Double?,
    val startedElapsedNs: Long?,
    val endedElapsedNs: Long?
)

/**
 * One telemetry sample. Unsupported values MUST be null (never 0).
 * Dual timeline: [utcEpochMs] (merge with OAI) + [elapsedRealtimeNs] (ordering/energy).
 */
@Entity(tableName = "phone_samples")
data class PhoneSampleEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val utcEpochMs: Long?,
    val elapsedRealtimeNs: Long?,
    val experimentId: String?,
    val runId: String?,
    val conditionId: String?,
    val sessionId: String?,
    val deviceId: String?,
    val phase: String?,
    // battery
    val batteryCurrentNowUa: Double?,
    val batteryCurrentAverageUa: Double?,
    val batteryVoltageMv: Double?,
    val batteryPowerW: Double?,
    val chargeCounterUah: Double?,
    val socPercent: Double?,
    val batteryTemperatureC: Double?,
    val thermalStatus: Int?,
    val thermalHeadroom: Double?,
    // NR
    val ssRsrpDbm: Double?,
    val ssRsrqDb: Double?,
    val ssSinrDb: Double?,
    val csiRsrpDbm: Double?,
    val csiRsrqDb: Double?,
    val csiSinrDb: Double?,
    val csiCqi: Int?,
    val nrarfcn: Int?,
    val pci: Int?,
    val nci: String?,
    val tac: Int?,
    val networkType: String?,
    // confounders
    val screenState: String?,
    val plugged: Int?,
    val charging: Int?,
    val wifiState: String?,
    val bluetoothState: String?,
    val airplaneMode: Int?,
    // workload
    val workloadType: String?,
    val workloadTargetMbps: Double?,
    val workloadActualMbps: Double?,
    val appTxBytes: Long?,
    val appRxBytes: Long?,
    val sampleQualityFlags: String?
)

@Entity(tableName = "event_markers")
data class EventMarkerEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val utcEpochMs: Long?,
    val elapsedRealtimeNs: Long?,
    val experimentId: String?,
    val runId: String?,
    val conditionId: String?,
    val markerType: String,
    val payloadJson: String?
)

@Entity(tableName = "sync_anchors")
data class SyncAnchorEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val direction: String,           // before | after
    val attemptIndex: Int,
    val t1Ms: Long?,                 // PC send (PC clock)
    val t2UtcMs: Long?,              // phone receive (phone UTC)
    val t2ElapsedNs: Long?,          // phone receive (phone monotonic)
    val t3Ms: Long?                  // PC receive (PC clock)
)

/* ----------------------------------------------------------------------- DAOs */

@Dao
interface SampleDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(sample: PhoneSampleEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(samples: List<PhoneSampleEntity>)

    @Query("SELECT * FROM phone_samples WHERE runId = :runId ORDER BY elapsedRealtimeNs ASC")
    suspend fun byRun(runId: String): List<PhoneSampleEntity>

    @Query("DELETE FROM phone_samples WHERE runId = :runId")
    suspend fun deleteRun(runId: String)

    /** Discard a monitoring session that never received a platform run id. */
    @Query("DELETE FROM phone_samples WHERE experimentId = :eid AND runId IS NULL")
    suspend fun deleteUnarmed(eid: String): Int

    /** Per-run summary for the USB data inventory (agent /agent/data/inventory). */
    @Query(
        "SELECT runId AS run_id, experimentId AS experiment_id, conditionId AS condition_id," +
            " COUNT(*) AS sample_count, MIN(utcEpochMs) AS first_utc_ms, MAX(utcEpochMs) AS last_utc_ms" +
            " FROM phone_samples WHERE runId IS NOT NULL AND runId != ''" +
            " GROUP BY runId ORDER BY MIN(utcEpochMs) ASC"
    )
    suspend fun runSummaries(): List<RunSummary>
}

/** Aggregate row returned by [SampleDao.runSummaries]. */
data class RunSummary(
    val run_id: String,
    val experiment_id: String?,
    val condition_id: String?,
    val sample_count: Int,
    val first_utc_ms: Long?,
    val last_utc_ms: Long?
)

@Dao
interface MarkerDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(marker: EventMarkerEntity)

    @Query("SELECT * FROM event_markers WHERE runId = :runId ORDER BY elapsedRealtimeNs ASC")
    suspend fun byRun(runId: String): List<EventMarkerEntity>

    /** Discard a monitoring session that never received a platform run id. */
    @Query("DELETE FROM event_markers WHERE experimentId = :eid AND runId IS NULL")
    suspend fun deleteUnarmed(eid: String): Int
}

@Dao
interface SyncDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(anchor: SyncAnchorEntity)

    @Query("SELECT * FROM sync_anchors WHERE direction = :direction ORDER BY attemptIndex ASC")
    suspend fun byDirection(direction: String): List<SyncAnchorEntity>

    @Query("DELETE FROM sync_anchors WHERE direction = :direction")
    suspend fun clearDirection(direction: String)
}

@Dao
interface MetaDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertDevice(device: DeviceInfoEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertExperiment(experiment: ExperimentEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertSession(session: SessionEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertCondition(condition: ConditionEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertRun(run: RunEntity)

    @Query("SELECT * FROM device_info LIMIT 1")
    suspend fun device(): DeviceInfoEntity?

    @Query("SELECT * FROM runs ORDER BY startedElapsedNs DESC LIMIT 1")
    suspend fun latestRun(): RunEntity?

    @Query("SELECT * FROM runs WHERE runId = :runId LIMIT 1")
    suspend fun run(runId: String): RunEntity?
}

/* ------------------------------------------------------------------- Database */

@Database(
    entities = [
        DeviceInfoEntity::class, ExperimentEntity::class, SessionEntity::class,
        ConditionEntity::class, RunEntity::class, PhoneSampleEntity::class,
        EventMarkerEntity::class, SyncAnchorEntity::class
    ],
    version = 1,
    exportSchema = false
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun samples(): SampleDao
    abstract fun markers(): MarkerDao
    abstract fun sync(): SyncDao
    abstract fun meta(): MetaDao

    companion object {
        @Volatile private var INSTANCE: AppDatabase? = null

        fun get(context: Context): AppDatabase =
            INSTANCE ?: synchronized(this) {
                INSTANCE ?: Room.databaseBuilder(
                    context.applicationContext, AppDatabase::class.java, "energy_agent.db"
                ).fallbackToDestructiveMigration().build().also { INSTANCE = it }
            }
    }
}

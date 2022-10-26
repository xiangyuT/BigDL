/*
 * Copyright 2016 The BigDL Authors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.intel.analytics.bigdl.friesian;

import com.google.protobuf.Empty;
import com.intel.analytics.bigdl.friesian.nearline.feature.FeatureInitializer;
import com.intel.analytics.bigdl.friesian.nearline.recall.RecallInitializer;
import com.intel.analytics.bigdl.friesian.nearline.utils.NearlineUtils;
import com.intel.analytics.bigdl.friesian.serving.feature.FeatureServer;
import com.intel.analytics.bigdl.friesian.serving.grpc.generated.recall.RecallGrpc;
import com.intel.analytics.bigdl.friesian.serving.grpc.generated.recall.RecallProto;
import com.intel.analytics.bigdl.friesian.serving.recall.RecallServer;
import com.intel.analytics.bigdl.friesian.serving.utils.Utils;
import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import io.grpc.StatusRuntimeException;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;
import org.junit.jupiter.api.*;

import java.util.Objects;
import java.util.concurrent.TimeUnit;

import static com.intel.analytics.bigdl.friesian.JavaTestUtils.destroyLettuceUtilsInstance;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
public class RecallServerTest {
    private static final Logger logger = LogManager.getLogger(RecallServerTest.class.getName());
    private static RecallGrpc.RecallBlockingStub recallBlockingStub;
    private static ManagedChannel channel;
    private static int insertedItemID;
    private static Row userDataRow;
    private static FeatureServer featureVecServer;
    private static RecallServer recallServer;


    /**
     * Sets up the test fixture.
     * (Called before every test case method.)
     */
    @BeforeAll
    public static void setUp() throws Exception {

        String configDir = Objects.requireNonNull(
                RecallServerTest.class.getClassLoader().getResource("testConfig")).getPath();
        FeatureInitializer.main(new String[]{"-c", configDir + "/config_feature_vec_init.yaml"});
        destroyLettuceUtilsInstance();

        SparkSession sparkSession = SparkSession.builder().getOrCreate();
        userDataRow = sparkSession.read().parquet(NearlineUtils.helper().getInitialUserDataPath()).first();
        RecallInitializer.main(new String[]{"-c", configDir + "/config_recall_init.yaml"});

        featureVecServer = new FeatureServer(
                new String[]{"-c", configDir + "/config_feature_vec_server.yaml"});
        featureVecServer.parseConfig();
        featureVecServer.build();
        featureVecServer.start();
        destroyLettuceUtilsInstance();


        recallServer = new RecallServer(new String[]{"-c", configDir + "/config_recall_server.yaml"});
        recallServer.parseConfig();
        recallServer.build();
        recallServer.start();
        channel = ManagedChannelBuilder.forAddress(
                "localhost", Utils.helper().getServicePort()).usePlaintext().build();
        recallBlockingStub = RecallGrpc.newBlockingStub(channel);
    }

    /**
     * Tears down the test fixture.
     * (Called after every test case method.)
     */
    @AfterAll
    public static void tearDown() throws InterruptedException {
        channel.shutdownNow().awaitTermination(5, TimeUnit.SECONDS);
        featureVecServer.stop();
        recallServer.stop();
    }

    @Test
    @Order(1)
    public void testAddItem() {
        insertedItemID = (int) (Math.random() * 1000) + 30000;
        RecallProto.Item.Builder itemBuilder = RecallProto.Item.newBuilder().setItemID(insertedItemID);
        float[] insertedItemEbd = TestUtils.toFloatArray(userDataRow.get(1));
        for (float x : insertedItemEbd) {
            itemBuilder.addItemVector(x);
        }
        Empty empty = recallBlockingStub.addItem(itemBuilder.build());
        assertNotNull(empty);
    }

    @Test
    @Order(2)
    public void testSearchCandidates() {
        RecallProto.Query query = RecallProto.Query.newBuilder().
                setUserID(Integer.parseInt(userDataRow.get(0).toString()))
                .setK(4).build();
        RecallProto.Candidates itemIDs = recallBlockingStub.searchCandidates(query);
        assertEquals(itemIDs.getCandidate(0), insertedItemID);
        assertEquals(itemIDs.getCandidateCount(), 4);
    }


    @Test
    public void testGetMetrics() {
        Empty request = Empty.newBuilder().build();

        RecallProto.ServerMessage msg = null;
        try {
            msg = recallBlockingStub.getMetrics(request);
        } catch (StatusRuntimeException e) {
            logger.error("RPC failed:" + e.getStatus());
        }
        assertNotNull(msg);
        assertNotNull(msg.getStr());
        logger.info("Got metrics: " + msg.getStr());
    }

    @Test
    public void testResetMetrics() {
        Empty request = Empty.newBuilder().build();
        Empty empty = null;
        try {
            empty = recallBlockingStub.resetMetrics(request);
        } catch (StatusRuntimeException e) {
            logger.error("RPC failed: " + e.getStatus().toString());
        }
        assertNotNull(empty);
    }
}

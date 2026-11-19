/*
 * PatroLink · ESP32-S3 链路探针节点（v0.1）
 * ------------------------------------------------------------
 * 作用：把"真实 WiFi 链路"变成可测量的数据。
 *
 *   1) UDP 回显（端口 9000）：PC 发 seq + t0，板子回 seq/t0/t1/t2/rssi，
 *      PC 侧用四时间戳算 RTT = (t3-t0) - (t2-t1)，不需要对时。
 *   2) HTTP /rssi  ：当前 RSSI / 信道 / IP
 *   3) HTTP /info  ：芯片、堆、PSRAM、运行时长
 *   4) HTTP /sink  ：POST 任意二进制（后续传 JPEG），返回收到字节数与耗时 → 上行吞吐
 *   5) HTTP /echo?kb=N：返回 N KB 数据 → 下行吞吐
 *
 * 编译环境：Arduino IDE 2.x + ESP32 core 3.x
 *   开发板：ESP32S3 Dev Module
 *   USB CDC On Boot: Enabled（否则串口无输出）
 *   PSRAM: 有就选 OPI PSRAM，没有就 Disabled
 *
 * 使用前只改下面这一处 WIFI_SSID / WIFI_PASS（用手机热点或 PC 移动热点）。
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>

// ======== 只改这里 ========
#define WIFI_SSID "PatroLink-AP"     // 换成你的热点名
#define WIFI_PASS "12345678"         // 热点密码（至少 8 位）
// =========================

#define UDP_PORT   9000
#define HTTP_PORT  80
#define SERIAL_BAUD 115200

WiFiUDP udp;
WebServer server(HTTP_PORT);

// /sink 上传统计
volatile size_t   sink_bytes = 0;
volatile uint32_t sink_t0_ms = 0;
volatile uint32_t sink_last_ms = 0;

static const char *jsonHeader = "application/json";

// ---------------- UDP 回显：四时间戳 ----------------
// 请求 12 字节：seq(uint32) + t0_us(int64)
// 应答 32 字节：seq(uint32) + t0_us(int64) + t1_us(int64) + t2_us(int64) + rssi(int16) + pad(2)
void handleUdpProbe() {
  uint8_t buf[12];
  int n = udp.read(buf, sizeof(buf));
  if (n != (int)sizeof(buf)) return;

  uint32_t seq;
  int64_t t0_us;
  memcpy(&seq, buf, 4);
  memcpy(&t0_us, buf + 4, 8);

  int64_t t1_us = esp_timer_get_time();
  int16_t rssi = (int16_t)(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);

  uint8_t out[32];
  memset(out, 0, sizeof(out));
  memcpy(out, &seq, 4);
  memcpy(out + 4, &t0_us, 8);
  memcpy(out + 12, &t1_us, 8);
  int64_t t2_us = esp_timer_get_time();
  memcpy(out + 20, &t2_us, 8);
  memcpy(out + 28, &rssi, 2);

  udp.beginPacket(udp.remoteIP(), udp.remotePort());
  udp.write(out, sizeof(out));
  udp.endPacket();
}

// ---------------- HTTP ----------------
void handleRssi() {
  char buf[220];
  snprintf(buf, sizeof(buf),
           "{\"rssi\":%d,\"ch\":%d,\"ip\":\"%s\",\"rtt_probe\":\"udp:%d\",\"ms\":%lu}",
           WiFi.RSSI(), WiFi.channel(), WiFi.localIP().toString().c_str(),
           UDP_PORT, (unsigned long)millis());
  server.send(200, jsonHeader, buf);
}

void handleInfo() {
  char buf[320];
  snprintf(buf, sizeof(buf),
           "{\"chip\":\"%s\",\"cores\":%d,\"freq_mhz\":%d,\"heap\":%u,\"psram\":%u,"
           "\"flash_mb\":%u,\"uptime_s\":%lu,\"rssi\":%d}",
           ESP.getChipModel(), ESP.getChipCores(), getCpuFrequencyMhz(),
           (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getPsramSize(),
           (unsigned)(ESP.getFlashChipSize() / (1024 * 1024)),
           (unsigned long)(millis() / 1000), WiFi.RSSI());
  server.send(200, jsonHeader, buf);
}

// 流式接收，不把整块 JPEG 留在内存里（板子 RAM 有限）
void sinkUpload() {
  HTTPUpload &up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    sink_bytes = 0;
    sink_t0_ms = millis();
  } else if (up.status == UPLOAD_FILE_WRITE) {
    sink_bytes += up.currentSize;
    sink_last_ms = millis();
  }
}

void sinkDone() {
  uint32_t dt = millis() - sink_t0_ms;
  char buf[160];
  snprintf(buf, sizeof(buf),
           "{\"bytes\":%u,\"ms\":%u,\"rssi\":%d,\"kbps\":%u}",
           (unsigned)sink_bytes, (unsigned)dt, WiFi.RSSI(),
           (unsigned)(dt ? (uint64_t)sink_bytes * 8 / dt : 0));
  server.send(200, jsonHeader, buf);
}

// 返回 N KB 'A'，测下行吞吐
void handleEcho() {
  size_t kb = server.hasArg("kb") ? (size_t)server.arg("kb").toInt() : 8;
  if (kb == 0) kb = 1;
  if (kb > 512) kb = 512;
  size_t total = kb * 1024;
  server.setContentLength(total);
  server.send(200, "application/octet-stream", "");
  WiFiClient client = server.client();
  uint8_t chunk[1024];
  memset(chunk, 'A', sizeof(chunk));
  while (total > 0) {
    size_t n = total > sizeof(chunk) ? sizeof(chunk) : total;
    client.write(chunk, n);
    total -= n;
  }
}

// ---------------- WiFi ----------------
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // 关省电，RTT 更稳定（探针必须）
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[wifi] connecting to %s", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(400);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[wifi] OK ip=%s rssi=%d ch=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI(), WiFi.channel());
  } else {
    Serial.println("\n[wifi] FAILED, retrying in loop");
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(300);
  Serial.println("\n=== PatroLink ESP32-S3 link probe v0.1 ===");

  connectWiFi();
  udp.begin(UDP_PORT);

  server.on("/rssi", HTTP_GET, handleRssi);
  server.on("/info", HTTP_GET, handleInfo);
  server.on("/echo", HTTP_GET, handleEcho);
  server.on("/sink", HTTP_POST, sinkDone, sinkUpload);
  server.begin();
  Serial.printf("[http] listening on %d, udp probe on %d\n", HTTP_PORT, UDP_PORT);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    static uint32_t lastTry = 0;
    if (millis() - lastTry > 5000) {
      lastTry = millis();
      Serial.println("[wifi] reconnecting...");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
  } else {
    // HTTP
    server.handleClient();
    // UDP 探针
    int sz = udp.parsePacket();
    if (sz > 0) handleUdpProbe();
    // 心跳打印
    static uint32_t lastPrint = 0;
    if (millis() - lastPrint > 2000) {
      lastPrint = millis();
      Serial.printf("[hb] rssi=%d ch=%d ip=%s heap=%u up=%lus\n",
                    WiFi.RSSI(), WiFi.channel(), WiFi.localIP().toString().c_str(),
                    (unsigned)ESP.getFreeHeap(), (unsigned long)(millis() / 1000));
    }
  }
  delay(1);
}

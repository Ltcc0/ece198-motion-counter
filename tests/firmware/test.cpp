#include <cassert>
#include <cstdint>
#include <deque>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <limits>
#include "EEPROM.h"
constexpr int A0=14, LOW=0, HIGH=1, INPUT=0, OUTPUT=1;
uint32_t clockMs=0, echoWidth=0;
uint32_t millis() { return clockMs; }
uint32_t pulseIn(int,int,uint32_t) { return echoWidth; }
void pinMode(int,int) {}
void digitalWrite(int,int) {}
void delayMicroseconds(int) {}
void analogReadResolution(int) {}
int analogRead(int) { return 512; }
struct SerialClass {
  bool connected=true;
  std::deque<char> in;
  std::ostringstream out;
  explicit operator bool() const { return connected; }
  void begin(int) {}
  int available() const { return in.size(); }
  int read() { char c=in.front(); in.pop_front(); return c; }
  template<class T> void print(T value) { out<<value; }
  void print(float value,int precision) { out<<std::fixed<<std::setprecision(precision)<<value; }
  template<class T> void println(T value) { print(value); out<<'\n'; }
  void feed(std::string s) { for(char c:s) in.push_back(c); }
  std::string text() { return out.str(); }
  void clear() { out.str(""); out.clear(); }
} Serial;
// Firmware copy changes only unsigned long to uint32_t, matching UNO R4 width.
#include "firmware_target_width.inc"
void reboot(bool erase=false) {
  if(erase) EEPROM.bytes.fill(255);
  EEPROM.writes=0; EEPROM.cutAfter=-1;
  phase=WAIT_CLEAR; running=distanceValid=serialWasConnected=false;
  nearStreak=0; clearActive=false; clearStartedAt=0;
  sensorStatus="NO_ECHO"; distanceCm=0;
  nearCm=FIXED_NEAR_CM; farCm=FIXED_NEAR_CM+10;
  count=lastSavedId=sessionId=finalSeconds=0;
  startedAt=lastSampleAt=lastDisplayAt=0;
  commandLength=0; commandOverflow=false;
  Serial.in.clear(); Serial.clear(); Serial.connected=true;
  clockMs=echoWidth=0; setup();
}
void cmd(std::string s) { Serial.feed(s); readCommands(); }
void sampleAt(float cm,uint32_t when) {
  clockMs=when; echoWidth=cm<0?0:static_cast<uint32_t>(cm*58); sampleDistance();
}
void samples(float cm,int n=3) { for(int i=0;i<n;++i) sampleAt(cm,clockMs+SAMPLE_MS); }
void cycle() { samples(40,6); samples(10); }
int occurrences(const std::string &s,const std::string &needle) {
  int n=0; for(size_t i=0;(i=s.find(needle,i))!=std::string::npos;i+=needle.size()) ++n; return n;
}
void recordAt(uint32_t id,Record &r) { EEPROM.get(recordAddress((id-1)%LOG_SLOTS),r); }
int main() {
  reboot(true); cmd("START\n"); assert(running && sessionId==1);
  samples(10,20); assert(count==0 && phase==WAIT_CLEAR); // blocked at START
  samples(40,5); assert(phase==WAIT_CLEAR); // 280 ms since first clear
  samples(40,1); assert(phase==ARMED && count==0);
  samples(25,2); assert(count==0 && phase==ARMED);
  samples(25,1); assert(count==1 && phase==OCCUPIED); // count on ENTRY
  samples(10,50); assert(count==1 && phase==OCCUPIED);
  samples(30,20); assert(count==1 && phase==OCCUPIED); // hysteresis deadband
  for(int i=0;i<5;++i) cycle(); assert(count==6);
  assert(occurrences(Serial.text(),"EVENT=PASS_COUNTED")==6);
  std::cout<<"PASS blocked START, clear confirmation, three-sample entry, hold, repeated passes\n";

  reboot(true); cmd("START\n"); samples(-1,6); assert(phase==ARMED && count==0);
  samples(10); assert(count==1 && phase==OCCUPIED);
  samples(-1,4); assert(phase==OCCUPIED); // short dropout cannot rearm
  samples(10,20); assert(count==1 && phase==OCCUPIED);
  samples(-1,6); assert(phase==ARMED && count==1);
  samples(10); assert(count==2 && phase==OCCUPIED);
  samples(81,6); assert(phase==ARMED); samples(10); assert(count==3);
  Serial.clear(); samples(-1,100); assert(count==3 && phase==ARMED);
  assert(occurrences(Serial.text(),"EVENT=CLEARED")==1);
  reboot(true); cmd("START\n"); samples(-1,100); assert(count==0 && phase==ARMED);
  assert(occurrences(Serial.text(),"EVENT=CLEAR_READY")==1);
  printStatus(); assert(Serial.text().find("DIST_CM=NO_TARGET SENSOR=NO_ECHO")!=std::string::npos);
  samples(1,1); Serial.clear(); printStatus();
  assert(Serial.text().find("SENSOR=TOO_CLOSE")!=std::string::npos);
  samples(81,1); Serial.clear(); printStatus();
  assert(Serial.text().find("SENSOR=TOO_FAR")!=std::string::npos);
  std::cout<<"PASS brief dropout, sustained no-echo/far clear, no-echo-only zero count, status reasons\n";

  reboot(true); cmd("START\n"); samples(40,6);
  samples(10,2); samples(-1,1); samples(10,1); assert(count==0);
  samples(10,2); assert(count==1 && phase==OCCUPIED);
  samples(40,5); samples(30,1); samples(40,5); assert(phase==OCCUPIED);
  samples(1,1); samples(40,5); assert(phase==OCCUPIED); // too-close interrupts clear
  samples(10,1); samples(1,30); assert(phase==OCCUPIED && count==1);
  samples(40,6); assert(phase==ARMED);
  for(int i=0;i<20;++i) { samples(10,2); samples(40,2); }
  assert(count==1 && phase==ARMED); // rapid incomplete entries never accumulate
  samples(10,2); samples(30,1); samples(10,1); assert(count==1);
  samples(10,2); assert(count==2);
  for(int i=0;i<20;++i) { samples(40,4); samples(10,3); }
  assert(count==2 && phase==OCCUPIED); // short clear periods never accumulate
  std::cout<<"PASS broken near/clear streaks, too-close exclusion, rapid oscillation\n";

  reboot(true); cmd("START\n"); sampleAt(40,0); assert(clearActive);
  sampleAt(40,299); assert(phase==WAIT_CLEAR);
  sampleAt(30,300); assert(phase==WAIT_CLEAR && !clearActive); // current sample wins
  sampleAt(40,400); sampleAt(40,699); assert(phase==WAIT_CLEAR);
  sampleAt(40,700); assert(phase==ARMED); // exact 300 ms boundary
  samples(10); assert(count==1);
  cmd("STOP\nSTART\n"); assert(phase==WAIT_CLEAR && !clearActive);
  samples(10,30); assert(count==0 && phase==WAIT_CLEAR); // no old-session arm state
  reboot(true); clockMs=UINT32_MAX-100; cmd("START\n");
  sampleAt(40,clockMs); sampleAt(40,198); assert(phase==WAIT_CLEAR);
  sampleAt(40,199); assert(phase==ARMED); samples(10); assert(count==1);
  std::cout<<"PASS exact clear timeout, current-sample cancellation, START reset, clear timer rollover\n";

  reboot(true); cmd("START\n"); cycle();
  clockMs=12345; cmd("STOP\r\n"); assert(!running && lastSavedId==1 && finalSeconds==12);
  assert(occurrences(Serial.text(),"SAVED id=")==1);
  int writes=EEPROM.writes; cmd("STOP\n"); assert(EEPROM.writes==writes);
  Record r; recordAt(1,r); assert(validRecord(r)&&r.count==1&&r.seconds==12);
  reboot(); assert(lastSavedId==1); cmd("LOG\n"); assert(Serial.text().find("1,1,12\n")!=std::string::npos);
  cmd("START\n"); assert(sessionId==2); cycle(); reboot(); assert(lastSavedId==1);
  std::cout<<"PASS STOP, CRLF single save, duplicate STOP, persistence, unfinished session\n";

  reboot(true); cmd("  sTaRt \t\r\n"); assert(running && sessionId==1);
  assert(occurrences(Serial.text(),"STARTED id=")==1);
  cmd("STOP\n"); Serial.clear();
  cmd(std::string(48,'X')+"START\r\n"); assert(!running);
  assert(occurrences(Serial.text(),"Command too long")==1);
  cmd("START\n"); assert(running&&sessionId==2);
  Serial.feed("ST"); readCommands(); assert(commandLength==2);
  serialWasConnected=true; Serial.connected=false; loop(); assert(commandLength==0);
  Serial.connected=true; cmd("OP\n"); assert(running); // old partial command discarded
  std::cout<<"PASS parser case/whitespace, CRLF, overlong rejection/recovery, disconnect\n";

  reboot(true); clockMs=UINT32_MAX-499; cmd("START\n");
  clockMs=1500; assert(elapsedSeconds()==2); cmd("STOP\n"); assert(finalSeconds==2);
  lastSavedId=UINT32_MAX; cmd("START\n"); assert(!running);
  reboot(true); cmd("START\n"); count=UINT32_MAX; cycle(); assert(count==UINT32_MAX);
  std::cout<<"PASS millis rollover (32-bit target emulation), session ID limit, count saturation\n";

  reboot(true);
  for(int id=1;id<=20;++id) { cmd("START\n"); count=id; clockMs+=3000; cmd("STOP\n"); }
  reboot(); assert(lastSavedId==20); Serial.clear(); cmd("LOG\n");
  auto log=Serial.text(); assert(log.find("5,5,3\n")!=std::string::npos && log.find("20,20,3\n")!=std::string::npos);
  assert(log.find("\n4,4,3\n")==std::string::npos);
  for(int id=5;id<=20;++id) { recordAt(id,r); assert(validRecord(r)&&r.id==id); }
  auto baseline=EEPROM.bytes;
  recordAt(20,r); int addr=recordAddress(19%16); EEPROM.bytes[addr+offsetof(Record,count)]^=1;
  reboot(); assert(lastSavedId==19); // corrupted newest rejected
  EEPROM.bytes=baseline; EEPROM.bytes[addr+offsetof(Record,committed)]=0;
  reboot(); assert(lastSavedId==19); // incomplete newest rejected
  std::cout<<"PASS 16-record wrap, chronological read, checksum/commit rejection\n";

  // Simulate power loss after every changed-byte write while replacing oldest slot.
  EEPROM.bytes=baseline; reboot(); cmd("START\n"); count=99;
  EEPROM.writes=0; cmd("STOP\n"); int totalWrites=EEPROM.writes;
  for(int cut=0;cut<=totalWrites;++cut) {
    EEPROM.bytes=baseline; reboot(); cmd("START\n"); count=99;
    EEPROM.writes=0; EEPROM.cutAfter=cut; bool interrupted=false;
    try { cmd("STOP\n"); } catch(PowerCut &) { interrupted=true; }
    reboot(); assert(lastSavedId==20 || lastSavedId==21);
    for(int id=6;id<=20;++id) { recordAt(id,r); assert(validRecord(r)&&r.id==id&&r.count==static_cast<uint32_t>(id)); }
    recordAt(21,r); if(validRecord(r)) assert((r.id==5&&r.count==5)||(r.id==21&&r.count==99));
    if(!interrupted) assert(lastSavedId==21);
  }
  std::cout<<"PASS "<<totalWrites+1<<" byte-write interruption points (host storage model only)\n";
  std::cout<<"ALL TESTS PASSED\n";
}

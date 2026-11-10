#pragma once
#include <array>
#include <cstdint>
#include <cstring>
#include <stdexcept>
struct PowerCut {};
struct EEPROMClass {
  std::array<uint8_t,8192> bytes;
  int writes=0, cutAfter=-1;
  EEPROMClass() { bytes.fill(255); }
  int length() const { return bytes.size(); }
  template<class T> void get(int address,T &value) {
    if (address<0 || address+sizeof(T)>bytes.size()) throw std::out_of_range("EEPROM read");
    std::memcpy(&value,bytes.data()+address,sizeof(T));
  }
  void update(int address,uint8_t value) {
    if (address<0 || address>=bytes.size()) throw std::out_of_range("EEPROM write");
    if (bytes[address]==value) return;
    if (cutAfter>=0 && writes==cutAfter) throw PowerCut{};
    bytes[address]=value; ++writes;
  }
  template<class T> void put(int address,const T &value) {
    auto p=reinterpret_cast<const uint8_t*>(&value);
    for (unsigned i=0;i<sizeof(T);++i) update(address+i,p[i]);
  }
};
inline EEPROMClass EEPROM;

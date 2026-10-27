/*
 * main.cpp
 * Drive 12 WB2812B on GP15
 * Produce a chaser pattern
 *
 *  Created on: 15 Jun 2021
 *      Author: jondurrant
 */


#include "pico/stdlib.h"
#include <stdexcept>
#include <cstring>
#include <PicoLed.hpp>
#include <tiny-json.h>
#include "TriTurn.h"

#define LEDS_PIN 18
#define LEDS_LENGTH 24


#define BUF_LEN 256
char buf[BUF_LEN];
int bufInx = 0;

#define MAX_FIELDS 10
json_t pool[MAX_FIELDS];

enum Modes {
	LISTEN,
	THINK,
	ON,
	OFF
};
Modes devMode = OFF;
bool on = false;
uint8_t dir = 0;


void handleJSON(){
	printf("Read(%d) %s\n", bufInx, buf);
	json_t const* parent = json_create( buf, pool, MAX_FIELDS );
	if ( parent == NULL ){
		printf("Not JSON\n");
		return;
	}

	json_t const* namefield = json_getProperty( parent, "mode" );
	if ( namefield == NULL ){
		printf("No mode\n");
		return;
	}
	char const* namevalue = json_getValue( namefield );
	if (strcmp(namevalue, "listen") == 0){
		namefield = json_getProperty( parent, "deg" );
		if ( namefield != NULL ){
			double deg = -1.0;
			if (json_getType(namefield) == JSON_INTEGER){
				deg = (double)json_getInteger(namefield);
			} else if (json_getType(namefield) == JSON_REAL){
				deg = json_getReal(namefield);
			}
			if (deg >=0){
				dir = ((double)LEDS_LENGTH/360.0) * deg;
				printf("Listen(%.0f) LED %d\n", deg, dir);
			}
		}
		devMode = LISTEN;
	} else if (strcmp(namevalue, "think") == 0){
		devMode = THINK;
	} else if (strcmp(namevalue, "on") == 0){
		devMode = ON;
		on = true;
	} else if (strcmp(namevalue, "off") == 0){
		devMode = OFF;
		on = false;
	} 
}

void stdioCB(void *param){
	int key = stdio_getchar_timeout_us(0);
	while (key != PICO_ERROR_TIMEOUT){
		if ((key == '\r') || (key == '\n')){
			buf[bufInx++] = 0; 
			handleJSON();
			bufInx = 0;
		} else {
			buf[bufInx++] = key;
		}
		key = stdio_getchar_timeout_us(0);
	}
}



int main(){

	stdio_init_all();
	sleep_ms(3000);
	printf("Go\n");
	stdio_set_chars_available_callback(stdioCB, NULL);

    // Initialize LED strip
	auto ledStrip = PicoLed::addLeds<PicoLed::WS2812B>(pio0, 0, LEDS_PIN, LEDS_LENGTH, PicoLed::FORMAT_GRB);

	//Keep brightness down as we are running at 3.3v
	ledStrip.setBrightness(64);

	// Initialise the LED to all off
	ledStrip.fill( PicoLed::RGB(0x00, 0x00, 0x00 ));
	ledStrip.show();
	sleep_ms(2000);

	// Set the chaser colour
	PicoLed::Color c = PicoLed::RGB(0xFF, 0, 0);

	// Setup Tri Turn
	c = PicoLed::RGB(0x00, 0x00, 0xFF);
	TriTurn * pTriTurn =  new TriTurn(
				&ledStrip,
				LEDS_LENGTH,
				c,
				100);


	for (;;){
		switch(devMode){
			case LISTEN:
				ledStrip.clear();
				ledStrip.setPixelColor(dir, PicoLed::RGB(0x00, 0xFF, 0x00 ));
				ledStrip.show();
				break;
			case THINK:
				pTriTurn->poll();
			break;
			default:
				if (on){
					ledStrip.fill(PicoLed::RGB(0xFF, 0xFF, 0xFF ));
				} else {
					ledStrip.clear();
				}
				ledStrip.show();
			break;
		}
		sleep_ms(10);
	}

}

from ttastromech import TTAstromech
import time


if __name__ == '__main__':
        r2 = TTAstromech()

        try:
#                r2.run()  # make random astromech sounds by feeding it random strings of letters
                r2.speak("hello world")
                time.sleep(2)
        except KeyboardInterrupt:
                print('bye ...')
            
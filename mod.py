import random
import config            
            
option = random.randint(1,3)
command_pres = config.PRES_A_SWITCH[option]
print(command_pres)
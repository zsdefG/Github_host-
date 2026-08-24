#include <stdlib.h>
#include <iostream>
#include <string>
using namespace std;

int main(){
    string choice;
    while (true)
    {
        cout<<"Please choose an option(https/dns/hosts): ";
        cin>>choice;
        if (choice=="https"){
        system("python accel_server.py proxy");
        } else if (choice=="dns"){
        system("python accel_server.py dns --port 5353");
        } else if (choice=="hosts"){
        system("python access_hosts.py");
        } else {
        cout<<"Invalid choice"<<endl;
        }
    }
} 

